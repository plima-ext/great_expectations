import configparser
import copy
import errno
import json
import logging
import os
import sys
import uuid
from abc import ABC, abstractmethod
from typing import Dict, List, Mapping, Optional, Union, cast

from ruamel.yaml.comments import CommentedMap

import great_expectations.exceptions as ge_exceptions
from great_expectations.core.usage_statistics.usage_statistics import (
    UsageStatisticsHandler,
)
from great_expectations.core.yaml_handler import YAMLHandler
from great_expectations.data_context.store import Store, TupleStoreBackend
from great_expectations.data_context.store.expectations_store import ExpectationsStore
from great_expectations.data_context.store.profiler_store import ProfilerStore
from great_expectations.data_context.store.validations_store import ValidationsStore
from great_expectations.data_context.types.base import (
    CURRENT_GE_CONFIG_VERSION,
    AnonymizedUsageStatisticsConfig,
    ConcurrencyConfig,
    DataContextConfig,
    DataContextConfigDefaults,
    DatasourceConfig,
    anonymizedUsageStatisticsSchema,
    datasourceConfigSchema,
)
from great_expectations.data_context.util import (
    PasswordMasker,
    build_store_from_config,
    instantiate_class_from_config,
    substitute_all_config_variables,
    substitute_config_variable,
)
from great_expectations.datasource import LegacyDatasource
from great_expectations.datasource.new_datasource import BaseDatasource, Datasource
from great_expectations.util import load_class, verify_dynamic_loading_support

logger = logging.getLogger(__name__)
yaml = YAMLHandler()


class AbstractDataContext(ABC):
    """
    Base class for all DataContexts that contain all context-agnostic data context operations.

    The class encapsulates most store / core components and convenience methods used to access them, meaning the
    majority of DataContext functionality lives here.
    """

    FALSEY_STRINGS = ["FALSE", "false", "False", "f", "F", "0"]
    GLOBAL_CONFIG_PATHS = [
        os.path.expanduser("~/.great_expectations/great_expectations.conf"),
        "/etc/great_expectations.conf",
    ]
    DOLLAR_SIGN_ESCAPE_STRING = r"\$"
    TEST_YAML_CONFIG_SUPPORTED_STORE_TYPES = [
        "ExpectationsStore",
        "ValidationsStore",
        "HtmlSiteStore",
        "EvaluationParameterStore",
        "MetricStore",
        "SqlAlchemyQueryStore",
        "CheckpointStore",
        "ProfilerStore",
    ]
    TEST_YAML_CONFIG_SUPPORTED_DATASOURCE_TYPES = [
        "Datasource",
        "SimpleSqlalchemyDatasource",
    ]
    TEST_YAML_CONFIG_SUPPORTED_DATA_CONNECTOR_TYPES = [
        "InferredAssetFilesystemDataConnector",
        "ConfiguredAssetFilesystemDataConnector",
        "InferredAssetS3DataConnector",
        "ConfiguredAssetS3DataConnector",
        "InferredAssetAzureDataConnector",
        "ConfiguredAssetAzureDataConnector",
        "InferredAssetGCSDataConnector",
        "ConfiguredAssetGCSDataConnector",
        "InferredAssetSqlDataConnector",
        "ConfiguredAssetSqlDataConnector",
    ]
    TEST_YAML_CONFIG_SUPPORTED_CHECKPOINT_TYPES = [
        "Checkpoint",
        "SimpleCheckpoint",
    ]
    TEST_YAML_CONFIG_SUPPORTED_PROFILER_TYPES = [
        "RuleBasedProfiler",
    ]
    ALL_TEST_YAML_CONFIG_DIAGNOSTIC_INFO_TYPES = [
        "__substitution_error__",
        "__yaml_parse_error__",
        "__custom_subclass_not_core_ge__",
        "__class_name_not_provided__",
    ]
    ALL_TEST_YAML_CONFIG_SUPPORTED_TYPES = (
        TEST_YAML_CONFIG_SUPPORTED_STORE_TYPES
        + TEST_YAML_CONFIG_SUPPORTED_DATASOURCE_TYPES
        + TEST_YAML_CONFIG_SUPPORTED_DATA_CONNECTOR_TYPES
        + TEST_YAML_CONFIG_SUPPORTED_CHECKPOINT_TYPES
        + TEST_YAML_CONFIG_SUPPORTED_PROFILER_TYPES
    )

    def __init__(self, runtime_environment: dict):
        """
        Constructor for AbstractDataContext. Will handle instantiation logic that is common to all DataContext objects

        Args:
            runtime_environment (dict): a dictionary of config variables that
                override both those set in config_variables.yml and the environment
        """
        self.runtime_environment = runtime_environment or {}
        # these attributes that are set downstream.
        self._config_variables = None

        # Init plugin support
        if self.plugins_directory is not None and os.path.exists(
            self.plugins_directory
        ):
            sys.path.append(self.plugins_directory)

        # We want to have directories set up before initializing usage statistics so that we can obtain a context instance id
        self._in_memory_instance_id = (
            None  # This variable *may* be used in case we cannot save an instance id
        )

        # Init stores
        self._stores = {}
        self._init_stores(self.project_config_with_variables_substituted.stores)

        # Init data_context_id
        self._data_context_id = self._construct_data_context_id()

        # Override the project_config data_context_id if an expectations_store was already set up
        self.config.anonymous_usage_statistics.data_context_id = self._data_context_id
        self._initialize_usage_statistics(
            self.project_config_with_variables_substituted.anonymous_usage_statistics
        )

        # Store cached datasources but don't init them
        self._cached_datasources = {}

        # Build the datasources we know about and have access to
        self._init_datasources()

    @abstractmethod
    def _init_variables(self) -> None:
        raise NotImplementedError

    def _construct_data_context_id(self) -> str:
        # Choose the id of the currently-configured expectations store, if it is a persistent store
        # overrridden in CloudDataContext : Note for Review
        expectations_store = self._stores[
            self.project_config_with_variables_substituted.expectations_store_name
        ]
        if isinstance(expectations_store.store_backend, TupleStoreBackend):
            # suppress_warnings since a warning will already have been issued during the store creation if there was an invalid store config
            return expectations_store.store_backend_id_warnings_suppressed

        # Otherwise choose the id stored in the project_config
        else:
            return (
                self.project_config_with_variables_substituted.anonymous_usage_statistics.data_context_id
            )

    # Properties
    @property
    def instance_id(self):
        instance_id = self.config_variables.get("instance_id")
        if instance_id is None:
            if self._in_memory_instance_id is not None:
                return self._in_memory_instance_id
            instance_id = str(uuid.uuid4())
            self._in_memory_instance_id = instance_id
        return instance_id

    # properties
    @property
    def config_variables(self) -> Dict:
        """Loads config variables into cache, by calling _load_config_variables()

        Returns: A dictionary containing config_variables from file or empty dictionary.
        """
        if not self._config_variables:
            self._config_variables = self._load_config_variables()
        return self._config_variables

    @property
    def config(self) -> DataContextConfig:
        return self._project_config

    @property
    def root_directory(self) -> Optional[str]:
        """The root directory for configuration objects in the data context; the location in which
        ``great_expectations.yml`` is located.

        Why does this exist in AbstractDataContext? CloudDataContext and FileDataContext both use it

        """
        if hasattr(self, "_context_root_directory"):
            return self._context_root_directory
        return

    @property
    def project_config_with_variables_substituted(self) -> DataContextConfig:
        return self.get_config_with_variables_substituted()

    @property
    def plugins_directory(self):
        """The directory in which custom plugin modules should be placed.

        Why does this exist in AbstractDataContext? CloudDataContext and FileDataContext both use it
        """
        return self._normalize_absolute_or_relative_path(
            self.project_config_with_variables_substituted.plugins_directory
        )

    @property
    def stores(self):
        """A single holder for all Stores in this context"""
        return self._stores

    @property
    def expectations_store_name(self) -> Optional[str]:
        return self.project_config_with_variables_substituted.expectations_store_name

    @property
    def expectations_store(self) -> ExpectationsStore:
        return self.stores[self.expectations_store_name]

    @property
    def evaluation_parameter_store_name(self):
        return (
            self.project_config_with_variables_substituted.evaluation_parameter_store_name
        )

    @property
    def evaluation_parameter_store(self):
        return self.stores[self.evaluation_parameter_store_name]

    @property
    def validations_store_name(self):
        return self.project_config_with_variables_substituted.validations_store_name

    @property
    def validations_store(self) -> ValidationsStore:
        return self.stores[self.validations_store_name]

    @property
    def checkpoint_store_name(self):
        try:
            return self.project_config_with_variables_substituted.checkpoint_store_name
        except AttributeError:
            from great_expectations.data_context.store.checkpoint_store import (
                CheckpointStore,
            )

            if CheckpointStore.default_checkpoints_exist(
                directory_path=self.root_directory
            ):
                return DataContextConfigDefaults.DEFAULT_CHECKPOINT_STORE_NAME.value
            if self.root_directory:
                error_message: str = f'Attempted to access the "checkpoint_store_name" field with no `checkpoints` directory.\n  Please create the following directory: {os.path.join(self.root_directory, DataContextConfigDefaults.DEFAULT_CHECKPOINT_STORE_BASE_DIRECTORY_RELATIVE_NAME.value)}\n  To use the new "Checkpoint Store" feature, please update your configuration to the new version number {float(CURRENT_GE_CONFIG_VERSION)}.\n  Visit https://docs.greatexpectations.io/docs/guides/miscellaneous/migration_guide#migrating-to-the-batch-request-v3-api to learn more about the upgrade process.'
            else:
                error_message: str = f'Attempted to access the "checkpoint_store_name" field with no `checkpoints` directory.\n  Please create a `checkpoints` directory in your Great Expectations project " f"directory.\n  To use the new "Checkpoint Store" feature, please update your configuration to the new version number {float(CURRENT_GE_CONFIG_VERSION)}.\n  Visit https://docs.greatexpectations.io/docs/guides/miscellaneous/migration_guide#migrating-to-the-batch-request-v3-api to learn more about the upgrade process.'
            raise ge_exceptions.InvalidTopLevelConfigKeyError(error_message)

    @property
    def checkpoint_store(self) -> "CheckpointStore":  # noqa: F821
        checkpoint_store_name: str = self.checkpoint_store_name
        try:
            return self.stores[checkpoint_store_name]
        except KeyError:
            from great_expectations.data_context.store.checkpoint_store import (
                CheckpointStore,
            )

            if CheckpointStore.default_checkpoints_exist(
                directory_path=self.root_directory
            ):
                logger.warning(
                    f'Checkpoint store named "{checkpoint_store_name}" is not a configured store, so will try to use default Checkpoint store.\n  Please update your configuration to the new version number {float(CURRENT_GE_CONFIG_VERSION)} in order to use the new "Checkpoint Store" feature.\n  Visit https://docs.greatexpectations.io/docs/guides/miscellaneous/migration_guide#migrating-to-the-batch-request-v3-api to learn more about the upgrade process.'
                )
                return self._build_store_from_config(
                    checkpoint_store_name,
                    DataContextConfigDefaults.DEFAULT_STORES.value[
                        checkpoint_store_name
                    ],
                )
            raise ge_exceptions.StoreConfigurationError(
                f'Attempted to access the Checkpoint store named "{checkpoint_store_name}", which is not a configured store.'
            )

    @property
    def profiler_store_name(self) -> str:
        try:
            return self.project_config_with_variables_substituted.profiler_store_name
        except AttributeError:
            if AbstractDataContext._default_profilers_exist(
                directory_path=self.root_directory
            ):
                return DataContextConfigDefaults.DEFAULT_PROFILER_STORE_NAME.value
            if self.root_directory:
                error_message: str = f'Attempted to access the "profiler_store_name" field with no `profilers` directory.\n  Please create the following directory: {os.path.join(self.root_directory, DataContextConfigDefaults.DEFAULT_PROFILER_STORE_BASE_DIRECTORY_RELATIVE_NAME.value)}\n  To use the new "Profiler Store" feature, please update your configuration to the new version number {float(CURRENT_GE_CONFIG_VERSION)}.\n  Visit https://docs.greatexpectations.io/docs/guides/miscellaneous/migration_guide#migrating-to-the-batch-request-v3-api to learn more about the upgrade process.'
            else:
                error_message: str = f'Attempted to access the "profiler_store_name" field with no `profilers` directory.\n  Please create a `profilers` directory in your Great Expectations project " f"directory.\n  To use the new "Profiler Store" feature, please update your configuration to the new version number {float(CURRENT_GE_CONFIG_VERSION)}.\n  Visit https://docs.greatexpectations.io/docs/guides/miscellaneous/migration_guide#migrating-to-the-batch-request-v3-api to learn more about the upgrade process.'
            raise ge_exceptions.InvalidTopLevelConfigKeyError(error_message)

    @property
    def profiler_store(self) -> ProfilerStore:
        profiler_store_name: str = self.profiler_store_name
        try:
            return self.stores[profiler_store_name]
        except KeyError:
            if AbstractDataContext._default_profilers_exist(
                directory_path=self.root_directory
            ):
                logger.warning(
                    f'Profiler store named "{profiler_store_name}" is not a configured store, so will try to use default Profiler store.\n  Please update your configuration to the new version number {float(CURRENT_GE_CONFIG_VERSION)} in order to use the new "Profiler Store" feature.\n  Visit https://docs.greatexpectations.io/docs/guides/miscellaneous/migration_guide#migrating-to-the-batch-request-v3-api to learn more about the upgrade process.'
                )
                built_store: Optional[Store] = self._build_store_from_config(
                    profiler_store_name,
                    DataContextConfigDefaults.DEFAULT_STORES.value[profiler_store_name],
                )
                return cast(ProfilerStore, built_store)

            raise ge_exceptions.StoreConfigurationError(
                f'Attempted to access the Profiler store named "{profiler_store_name}", which is not a configured store.'
            )

    @property
    def concurrency(self) -> Optional[ConcurrencyConfig]:
        return self.project_config_with_variables_substituted.concurrency

    def add_datasource(
        self, name: str, initialize: bool = True, **kwargs: dict
    ) -> Optional[Union[LegacyDatasource, BaseDatasource]]:
        """Add a new datasource to the data context, with configuration provided as kwargs.
        Args:
            name: the name for the new datasource to add
            initialize: if False, add the datasource to the config, but do not
                initialize it, for example if a user needs to debug database connectivity.
            kwargs (keyword arguments): the configuration for the new datasource

        Returns:
            datasource (Datasource)
        """
        logger.debug(f"Starting BaseDataContext.add_datasource for {name}")

        module_name: str = kwargs.get("module_name", "great_expectations.datasource")
        verify_dynamic_loading_support(module_name=module_name)
        class_name: Optional[str] = kwargs.get("class_name")
        datasource_class = load_class(module_name=module_name, class_name=class_name)

        # For any class that should be loaded, it may control its configuration construction
        # by implementing a classmethod called build_configuration
        config: Union[CommentedMap, dict]
        if hasattr(datasource_class, "build_configuration"):
            config = datasource_class.build_configuration(**kwargs)
        else:
            config = kwargs

        datasource: Optional[
            Union[LegacyDatasource, BaseDatasource]
        ] = self._instantiate_datasource_from_config_and_update_project_config(
            name=name,
            config=config,
            initialize=initialize,
        )
        return datasource

    def update_return_obj(self, data_asset, return_obj):
        """Helper called by data_asset.

        Args:
            data_asset: The data_asset whose validation produced the current return object
            return_obj: the return object to update

        Returns:
            return_obj: the return object, potentially changed into a widget by the configured expectation explorer
        """
        return return_obj

    def get_config_with_variables_substituted(
        self, config: Optional[DataContextConfig] = None
    ) -> DataContextConfig:
        """
        Substitute vars in config of form ${var} or $(var) with values found in the following places,
        in order of precedence: ge_cloud_config (for Data Contexts in GE Cloud mode), runtime_environment,
        environment variables, config_variables, or ge_cloud_config_variable_defaults (allows certain variables to
        be optional in GE Cloud mode).
        """
        if not config:
            config = self._project_config

        substitutions: dict = self._determine_substitutions()

        return DataContextConfig(
            **substitute_all_config_variables(
                config, substitutions, self.DOLLAR_SIGN_ESCAPE_STRING
            )
        )

    def list_stores(self):
        """List currently-configured Stores on this context"""

        stores = []
        for (
            name,
            value,
            # I wonder if it's because of the sql needing credentials and this not being loaded correctly?
        ) in self.project_config_with_variables_substituted.stores.items():
            store_config = copy.deepcopy(value)
            store_config["name"] = name
            masked_config = PasswordMasker.sanitize_config(store_config)
            stores.append(masked_config)
        return stores

    def list_active_stores(self):
        """
        List active Stores on this context. Active stores are identified by setting the following parameters:
            expectations_store_name,
            validations_store_name,
            evaluation_parameter_store_name,
            checkpoint_store_name
            profiler_store_name
        """
        active_store_names: List[str] = [
            self.expectations_store_name,
            self.validations_store_name,
            self.evaluation_parameter_store_name,
        ]

        try:
            active_store_names.append(self.checkpoint_store_name)
        except (AttributeError, ge_exceptions.InvalidTopLevelConfigKeyError):
            logger.info(
                "Checkpoint store is not configured; omitting it from active stores"
            )

        try:
            active_store_names.append(self.profiler_store_name)
        except (AttributeError, ge_exceptions.InvalidTopLevelConfigKeyError):
            logger.info(
                "Profiler store is not configured; omitting it from active stores"
            )

        return [
            store for store in self.list_stores() if store["name"] in active_store_names
        ]

    def get_datasource(
        self, datasource_name: str = "default"
    ) -> Optional[Union[LegacyDatasource, BaseDatasource]]:
        """Get the named datasource

        Args:
            datasource_name (str): the name of the datasource from the configuration

        Returns:
            datasource (Datasource)
        """
        if datasource_name in self._cached_datasources:
            return self._cached_datasources[datasource_name]

        datasource_config: DatasourceConfig = self._datasource_store.retrieve_by_name(
            datasource_name=datasource_name
        )

        config: dict = dict(datasourceConfigSchema.dump(datasource_config))
        substitutions: dict = self._determine_substitutions()
        config = substitute_all_config_variables(
            config, substitutions, self.DOLLAR_SIGN_ESCAPE_STRING
        )

        datasource: Optional[
            Union[LegacyDatasource, BaseDatasource]
        ] = self._instantiate_datasource_from_config(
            name=datasource_name, config=config
        )
        self._cached_datasources[datasource_name] = datasource
        return datasource

    def list_datasources(self) -> List[dict]:
        """List currently-configured datasources on this context. Masks passwords.

        Returns:
            List(dict): each dictionary includes "name", "class_name", and "module_name" keys
        """
        datasources: List[dict] = []
        substitutions: dict = self._determine_substitutions()
        datasource_name: str
        for datasource_name in self._datasource_store.list_keys():
            datasource_config: DatasourceConfig = (
                self._datasource_store.retrieve_by_name(datasource_name)
            )
            datasource_dict: dict = datasource_config.to_json_dict()
            datasource_dict["name"] = datasource_name
            substituted_config: dict = cast(
                dict,
                substitute_all_config_variables(
                    datasource_dict, substitutions, self.DOLLAR_SIGN_ESCAPE_STRING
                ),
            )
            masked_config: dict = PasswordMasker.sanitize_config(substituted_config)
            datasources.append(masked_config)
        return datasources

    def delete_datasource(self, datasource_name: str) -> None:
        """Delete a data source
        Args:
            datasource_name: The name of the datasource to delete.

        Raises:
            ValueError: If the datasource name isn't provided or cannot be found.
        """
        if datasource_name is None:
            raise ValueError("Datasource names must be a datasource name")
        else:
            datasource = self.get_datasource(datasource_name=datasource_name)
            if datasource:
                self._datasource_store.delete_by_name(datasource_name)
                del self._cached_datasources[datasource_name]
            else:
                raise ValueError(f"Datasource {datasource_name} not found")

    @staticmethod
    def _default_profilers_exist(directory_path: Optional[str]) -> bool:
        if not directory_path:
            return False

        profiler_directory_path: str = os.path.join(
            directory_path,
            DataContextConfigDefaults.DEFAULT_PROFILER_STORE_BASE_DIRECTORY_RELATIVE_NAME.value,
        )
        return os.path.isdir(profiler_directory_path)

    @staticmethod
    def _get_global_config_value(
        environment_variable: str,
        conf_file_section: Optional[str] = None,
        conf_file_option: Optional[str] = None,
    ) -> Optional[str]:
        """
        Method to retrieve config value.
        Looks for config value in environment_variable and config file section

        Args:
            environment_variable (str): name of environment_variable to retrieve
            conf_file_section (str): section of config
            conf_file_option (str): key in section

        Returns:
            Optional string representing config value
        """
        assert (conf_file_section and conf_file_option) or (
            not conf_file_section and not conf_file_option
        ), "Must pass both 'conf_file_section' and 'conf_file_option' or neither."
        if environment_variable and os.environ.get(environment_variable, False):
            return os.environ.get(environment_variable)
        if conf_file_section and conf_file_option:
            for config_path in AbstractDataContext.GLOBAL_CONFIG_PATHS:
                config = configparser.ConfigParser()
                config.read(config_path)
                config_value = config.get(
                    conf_file_section, conf_file_option, fallback=None
                )
                if config_value:
                    return config_value
        return None

    def _apply_global_config_overrides(
        self, config: Union[DataContextConfig, Mapping]
    ) -> DataContextConfig:

        """
        Applies global configuration overrides for
            - usage_statistics being enabled
            - data_context_id for usage_statistics
            - global_usage_statistics_url

        Args:
            config (DataContextConfig): Config that is passed into the DataContext constructor

        Returns:
            DataContextConfig with the appropriate overrides
        """
        validation_errors: dict = {}
        config_with_global_config_overrides: DataContextConfig = copy.deepcopy(config)
        usage_stats_opted_out: bool = self._check_global_usage_statistics_opt_out()
        # if usage_stats_opted_out then usage_statistics is false
        # TODO: Refactor so that this becomes usage_stats_enabled (and we don't have to flip the boolean in our minds)
        if usage_stats_opted_out:
            logger.info(
                "Usage statistics is disabled globally. Applying override to project_config."
            )
            config_with_global_config_overrides.anonymous_usage_statistics.enabled = (
                False
            )
        global_data_context_id: Optional[str] = self._get_data_context_id_override()
        # data_context_id
        if global_data_context_id:
            data_context_id_errors = anonymizedUsageStatisticsSchema.validate(
                {"data_context_id": global_data_context_id}
            )
            if not data_context_id_errors:
                logger.info(
                    "data_context_id is defined globally. Applying override to project_config."
                )
                config_with_global_config_overrides.anonymous_usage_statistics.data_context_id = (
                    global_data_context_id
                )
            else:
                validation_errors.update(data_context_id_errors)

        # usage statistics url
        global_usage_statistics_url: Optional[
            str
        ] = self._get_usage_stats_url_override()
        if global_usage_statistics_url:
            usage_statistics_url_errors = anonymizedUsageStatisticsSchema.validate(
                {"usage_statistics_url": global_usage_statistics_url}
            )
            if not usage_statistics_url_errors:
                logger.info(
                    "usage_statistics_url is defined globally. Applying override to project_config."
                )
                config_with_global_config_overrides.anonymous_usage_statistics.usage_statistics_url = (
                    global_usage_statistics_url
                )
            else:
                validation_errors.update(usage_statistics_url_errors)
        if validation_errors:
            logger.warning(
                "The following globally-defined config variables failed validation:\n{}\n\n"
                "Please fix the variables if you would like to apply global values to project_config.".format(
                    json.dumps(validation_errors, indent=2)
                )
            )

        return config_with_global_config_overrides

    def _load_config_variables(self) -> Dict:
        """
        Get all config variables from the default location. For Data Contexts in GE Cloud mode, config variables
        have already been interpolated before being sent from the Cloud API.

        """
        config_variables_file_path: str = cast(
            DataContextConfig,
            self._project_config,  # TODO: see if this can be resolved in a better way
        ).config_variables_file_path
        if config_variables_file_path:
            try:
                # If the user specifies the config variable path with an environment variable, we want to substitute it
                defined_path: str = substitute_config_variable(
                    config_variables_file_path, dict(os.environ)
                )
                if not os.path.isabs(defined_path) and hasattr(self, "root_directory"):
                    # A BaseDataContext will not have a root directory; in that case use the current directory
                    # for any non-absolute path
                    root_directory: str = self.root_directory or os.curdir
                else:
                    root_directory: str = ""
                var_path = os.path.join(root_directory, defined_path)
                with open(var_path) as config_variables_file:
                    res = dict(
                        yaml.load(config_variables_file)
                    )  # TODO this is returning TextIO directly. Adjust to return
                    # TextIOWrapper or str
                    return res or {}
            except OSError as e:
                if e.errno != errno.ENOENT:
                    raise
                logger.debug("Generating empty config variables file.")
                return {}
        else:
            return {}

    def _normalize_absolute_or_relative_path(
        self, path: Optional[str]
    ) -> Optional[str]:
        """
        Why does this exist in AbstractDataContext? CloudDataContext and FileDataContext both use it
        """
        if path is None:
            return
        if os.path.isabs(path):
            return path
        else:
            return os.path.join(self.root_directory, path)

    def _check_global_usage_statistics_opt_out(self) -> bool:
        """
        Method to retrieve config value.
        This method can be overridden in child classes (like FileDataContext) when we need to look for
        config values in other locations like config files.

        Returns:
            bool that tells you whether usage_statistics is opted out
        """
        if os.environ.get("GE_USAGE_STATS", False):
            ge_usage_stats = os.environ.get("GE_USAGE_STATS")
            if ge_usage_stats in AbstractDataContext.FALSEY_STRINGS:
                return True
            else:
                logger.warning(
                    "GE_USAGE_STATS environment variable must be one of: {}".format(
                        AbstractDataContext.FALSEY_STRINGS
                    )
                )
        for config_path in AbstractDataContext.GLOBAL_CONFIG_PATHS:
            config = configparser.ConfigParser()
            states = config.BOOLEAN_STATES
            for falsey_string in AbstractDataContext.FALSEY_STRINGS:
                states[falsey_string] = False
            states["TRUE"] = True
            states["True"] = True
            config.BOOLEAN_STATES = states
            config.read(config_path)
            try:
                if config.getboolean("anonymous_usage_statistics", "enabled") is False:
                    # If stats are disabled, then opt out is true
                    return True
            except (ValueError, configparser.Error):
                pass
        return False

    def _get_data_context_id_override(self) -> Optional[str]:
        """
        Return data_context_id from environment variable.

        Returns:
            Optional string that represents data_context_id
        """
        return self._get_global_config_value(
            environment_variable="GE_DATA_CONTEXT_ID",
            conf_file_section="anonymous_usage_statistics",
            conf_file_option="data_context_id",
        )

    def _get_usage_stats_url_override(self) -> Optional[str]:
        """
        Return GE_USAGE_STATISTICS_URL from environment variable if it exists

        Returns:
            Optional string that represents GE_USAGE_STATISTICS_URL
        """
        return self._get_global_config_value(
            environment_variable="GE_USAGE_STATISTICS_URL",
            conf_file_section="anonymous_usage_statistics",
            conf_file_option="usage_statistics_url",
        )

    def _build_store_from_config(
        self, store_name: str, store_config: dict
    ) -> Optional[Store]:
        module_name = "great_expectations.data_context.store"
        # Set expectations_store.store_backend_id to the data_context_id from the project_config if
        # the expectations_store does not yet exist by:
        # adding the data_context_id from the project_config
        # to the store_config under the key manually_initialize_store_backend_id
        if (store_name == self.expectations_store_name) and store_config.get(
            "store_backend"
        ):
            store_config["store_backend"].update(
                {
                    "manually_initialize_store_backend_id": self.project_config_with_variables_substituted.anonymous_usage_statistics.data_context_id
                }
            )

        # Set suppress_store_backend_id = True if store is inactive and has a store_backend.
        if (
            store_name not in [store["name"] for store in self.list_active_stores()]
            and store_config.get("store_backend") is not None
        ):
            store_config["store_backend"].update({"suppress_store_backend_id": True})

        new_store = build_store_from_config(
            store_name=store_name,
            store_config=store_config,
            module_name=module_name,
            runtime_environment={
                "root_directory": self.root_directory,
            },
        )
        self._stores[store_name] = new_store
        return new_store

    def _init_stores(self, store_configs: Dict[str, dict]) -> None:
        """Initialize all Stores for this DataContext.

        Stores are a good fit for reading/writing objects that:
            1. follow a clear key-value pattern, and
            2. are usually edited programmatically, using the Context

        Note that stores do NOT manage plugins.
        """
        for store_name, store_config in store_configs.items():
            self._build_store_from_config(store_name, store_config)

        # The DatasourceStore is inherent to all DataContexts but is not an explicit part of the project config.
        # As such, it must be instantiated separately.
        self._init_datasource_store()

    def _init_datasource_store(self) -> None:
        """Internal utility responsible for creating a DatasourceStore to persist and manage a user's Datasources.

        Please note that the DatasourceStore lacks the same extensibility that other analagous Stores do; a default
        implementation is provided based on the user's environment but is not customizable.
        """
        from great_expectations.data_context.store.datasource_store import (
            DatasourceStore,
        )

        store_name: str = "datasource_store"  # Never explicitly referenced but adheres to the convention set by other internal Stores
        store_backend: dict = {"class_name": "InlineStoreBackend"}
        runtime_environment: dict = {
            "root_directory": self.root_directory,
            "data_context": self,
            # By passing this value in our runtime_environment, we ensure that the same exact context (memory address and all) is supplied to the Store backend
        }

        datasource_store: DatasourceStore = DatasourceStore(
            store_name=store_name,
            store_backend=store_backend,
            runtime_environment=runtime_environment,
        )
        self._datasource_store = datasource_store

    def _update_config_variables(self) -> None:
        """Updates config_variables cache by re-calling _load_config_variables(). Necessary after running methods that modify config
        AND could contain config_variables for credentials (example is add_datasource())
        """
        self._config_variables = self._load_config_variables()

    def _determine_substitutions(self) -> dict:
        """Aggregates substitutions from the project's config variables file, any environment variables, and
        the runtime environment.

        Returns: A dictionary containing all possible substitutions that can be applied to a given object
                 using `substitute_all_config_variables`.
        """
        substituted_config_variables: dict = substitute_all_config_variables(
            self.config_variables,
            dict(os.environ),
            self.DOLLAR_SIGN_ESCAPE_STRING,
        )

        substitutions = {
            **substituted_config_variables,
            **dict(os.environ),
            **self.runtime_environment,
        }

        return substitutions

    def _initialize_usage_statistics(
        self, usage_statistics_config: AnonymizedUsageStatisticsConfig
    ) -> None:
        """Initialize the usage statistics system."""
        if not usage_statistics_config.enabled:
            logger.info("Usage statistics is disabled; skipping initialization.")
            self._usage_statistics_handler = None
            return

        self._usage_statistics_handler = UsageStatisticsHandler(
            data_context=self,
            data_context_id=self._data_context_id,
            usage_statistics_url=usage_statistics_config.usage_statistics_url,
        )

    def _init_datasources(self) -> None:
        for datasource_name in self._datasource_store.list_keys():
            try:
                datasource: Datasource = self.get_datasource(
                    datasource_name=datasource_name
                )
                self._cached_datasources[datasource_name] = datasource
            except ge_exceptions.DatasourceInitializationError as e:
                logger.warning(f"Cannot initialize datasource {datasource_name}: {e}")
                # this error will happen if our configuration contains datasources that GE can no longer connect to.
                # this is ok, as long as we don't use it to retrieve a batch. If we try to do that, the error will be
                # caught at the context.get_batch() step. So we just pass here.
                pass

    def _instantiate_datasource_from_config(
        self, name: str, config: Union[dict, DatasourceConfig]
    ) -> Union[LegacyDatasource, BaseDatasource]:
        """Instantiate a new datasource to the data context, with configuration provided as kwargs.
        Args:
            name(str): name of datasource
            config(dict): dictionary of configuration

        Returns:
            datasource (Datasource)
        """
        # We perform variable substitution in the datasource's config here before using the config
        # to instantiate the datasource object. Variable substitution is a service that the data
        # context provides. Datasources should not see unsubstituted variables in their config.

        try:
            datasource: Union[
                LegacyDatasource, BaseDatasource
            ] = self._build_datasource_from_config(name=name, config=config)
        except Exception as e:
            raise ge_exceptions.DatasourceInitializationError(
                datasource_name=name, message=str(e)
            )
        return datasource

    def _build_datasource_from_config(
        self, name: str, config: Union[dict, DatasourceConfig]
    ):
        # We convert from the type back to a dictionary for purposes of instantiation
        if isinstance(config, DatasourceConfig):
            config = datasourceConfigSchema.dump(config)
        config.update({"name": name})
        # While the new Datasource classes accept "data_context_root_directory", the Legacy Datasource classes do not.
        if config["class_name"] in [
            "BaseDatasource",
            "Datasource",
        ]:
            config.update({"data_context_root_directory": self.root_directory})
        module_name = "great_expectations.datasource"
        datasource = instantiate_class_from_config(
            config=config,
            runtime_environment={"data_context": self, "concurrency": self.concurrency},
            config_defaults={"module_name": module_name},
        )
        if not datasource:
            raise ge_exceptions.ClassInstantiationError(
                module_name=module_name,
                package_name=None,
                class_name=config["class_name"],
            )
        return datasource

    def _instantiate_datasource_from_config_and_update_project_config(
        self, name: str, config: dict, initialize: bool = True
    ) -> Optional[Union[LegacyDatasource, BaseDatasource]]:
        datasource_config: DatasourceConfig = datasourceConfigSchema.load(
            CommentedMap(**config)
        )

        self._datasource_store.set_by_name(
            datasource_name=name, datasource_config=datasource_config
        )

        # Config must be persisted with ${VARIABLES} syntax but hydrated at time of use
        substitutions: dict = self._determine_substitutions()
        config: dict = dict(datasourceConfigSchema.dump(datasource_config))
        substituted_config: dict = substitute_all_config_variables(
            config, substitutions, self.DOLLAR_SIGN_ESCAPE_STRING
        )

        datasource: Optional[Union[LegacyDatasource, BaseDatasource]] = None
        if initialize:
            try:
                datasource = self._instantiate_datasource_from_config(
                    name=name, config=substituted_config
                )
                self._cached_datasources[name] = datasource
            except ge_exceptions.DatasourceInitializationError as e:
                # Do not keep configuration that could not be instantiated.
                self._datasource_store.delete_by_name(datasource_name=name)
                raise e

        return datasource
