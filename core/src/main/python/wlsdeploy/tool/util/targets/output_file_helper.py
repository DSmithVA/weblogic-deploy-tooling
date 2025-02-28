"""
Copyright (c) 2022 Oracle Corporation and/or its affiliates.
Licensed under the Universal Permissive License v 1.0 as shown at https://oss.oracle.com/licenses/upl.

Methods to update an output file with information from the kubernetes section of the model.
"""
from java.io import File

from oracle.weblogic.deploy.util import PyOrderedDict
from oracle.weblogic.deploy.util import PyRealBoolean
from oracle.weblogic.deploy.yaml import YamlException

from wlsdeploy.aliases import alias_utils
from wlsdeploy.aliases.model_constants import KUBERNETES
from wlsdeploy.aliases.model_constants import MODEL_LIST_DELIMITER
from wlsdeploy.exception.expection_types import ExceptionType
from wlsdeploy.logging.platform_logger import PlatformLogger
from wlsdeploy.tool.extract import wko_schema_helper
from wlsdeploy.util import dictionary_utils
from wlsdeploy.yaml.yaml_translator import PythonToYaml
from wlsdeploy.yaml.yaml_translator import YamlToPython

__class_name = 'output_file_helper'
__logger = PlatformLogger('wlsdeploy.tool.util')

KIND = 'kind'
SPEC = 'spec'

WKO_DOMAIN_KIND = 'Domain'
DOMAIN_HOME = 'domainHome'

# specific to Verrazzano
COMPONENT_KIND = 'Component'
TEMPLATE = 'template'
VERRAZZANO_WEBLOGIC_WORKLOAD_KIND = 'VerrazzanoWebLogicWorkload'
WORKLOAD = 'workload'


def update_from_model(output_dir, output_file_name, model):
    """
    Update the output content with information from the kubernetes section of the model.
    Output files are (currently) always Kubernetes resource files.
    :param output_dir: the directory of the output file to update
    :param output_file_name: the name of the output file
    :param model: the model to use for update
    """
    _method_name = 'update_from_model'

    # if model doesn't have kubernetes section, return
    kubernetes_content = model.get_model_kubernetes()
    if not kubernetes_content:
        return

    # failures will be logged as severe, but not cause tool failure.
    # this will allow the unaltered output file to be examined for problems.

    output_file = File(output_dir, output_file_name)
    __logger.info('WLSDPLY-01675', output_file, KUBERNETES, class_name=__class_name, method_name=_method_name)

    try:
        reader = YamlToPython(output_file.getPath(), True)
        documents = reader.parse_documents()
    except YamlException, ex:
        __logger.severe('WLSDPLY-01673', output_file, str(ex), error=ex, class_name=__class_name,
                        method_name=_method_name)
        return

    _update_documents(documents, kubernetes_content, output_file.getPath())

    try:
        writer = PythonToYaml(documents)
        writer.write_to_yaml_file(output_file.getPath())
    except YamlException, ex:
        __logger.severe('WLSDPLY-01674', output_file, str(ex), error=ex, class_name=__class_name,
                        method_name=_method_name)
    return


def _update_documents(documents, kubernetes_content, output_file_path):
    _method_name = '_update_documents'
    found = False

    schema = wko_schema_helper.get_domain_resource_schema(ExceptionType.DEPLOY)

    # update section(s) based on their kind, etc.
    for document in documents:
        if isinstance(document, dict):
            kind = dictionary_utils.get_element(document, KIND)

            # is this a standard WKO document?
            if kind == WKO_DOMAIN_KIND:
                _update_dictionary(document, kubernetes_content, schema, None, output_file_path)
                found = True

            # is this a Verrazzano WebLogic workload document?
            elif kind == COMPONENT_KIND:
                spec = dictionary_utils.get_dictionary_element(document, SPEC)
                workload = dictionary_utils.get_dictionary_element(spec, WORKLOAD)
                component_kind = dictionary_utils.get_element(workload, KIND)
                if component_kind == VERRAZZANO_WEBLOGIC_WORKLOAD_KIND:
                    component_spec = _get_or_create_dictionary(workload, SPEC)
                    component_template = _get_or_create_dictionary(component_spec, TEMPLATE)
                    _update_dictionary(component_template, kubernetes_content, schema, None, output_file_path)
                    found = True

    if not found:
        __logger.warning('WLSDPLY-01676', output_file_path, class_name=__class_name, method_name=_method_name)


def _update_dictionary(output_dictionary, model_dictionary, schema_folder, schema_path, output_file_path):
    """
    Update output_dictionary with attributes from model_dictionary.
    :param output_dictionary: the dictionary to be updated
    :param model_dictionary: the dictionary to update from (type previously validated)
    :param schema_folder: the schema for this folder
    :param schema_path: used for wko_schema_helper lookups and logging
    :param output_file_path: used for logging
    """
    _method_name = '_update_dictionary'
    if not isinstance(output_dictionary, dict):
        __logger.warning('WLSDPLY-01677', schema_path, output_file_path, class_name=__class_name,
                         method_name=_method_name)
        return

    # no type checking for elements of simple (single type) map
    if wko_schema_helper.is_simple_map(schema_folder):
        for key, value in model_dictionary.items():
            output_dictionary[key] = value
        return

    properties = wko_schema_helper.get_properties(schema_folder)

    for key, value in model_dictionary.items():
        property_folder = properties[key]
        element_type = wko_schema_helper.get_type(property_folder)

        # deprecated "named object list" format
        value = _check_named_object_list(value, element_type, property_folder, schema_path, key)
        # end deprecated

        value = _convert_value(value, element_type)

        if key not in output_dictionary:
            output_dictionary[key] = value
        elif isinstance(value, dict):
            next_schema_path = wko_schema_helper.append_path(schema_path, key)
            _update_dictionary(output_dictionary[key], value, property_folder, next_schema_path, output_file_path)
        elif isinstance(value, list):
            if not value:
                # if the model has an empty list, override output value
                output_dictionary[key] = value
            else:
                next_schema_path = wko_schema_helper.append_path(schema_path, key)
                _update_list(output_dictionary[key], value, property_folder, next_schema_path, output_file_path)
        else:
            output_dictionary[key] = value


def _update_list(output_list, model_list, schema_folder, schema_path, output_file_path):
    """
    Update output_list from model_list, overriding or merging existing values
    :param output_list: the list to be updated
    :param model_list: the list to update from (type previously validated)
    :param schema_folder: the schema for members of this list
    :param schema_path: used for wko_schema_helper lookups and logging
    :param output_file_path: used for logging
    """
    _method_name = '_update_list'
    if not isinstance(output_list, list):
        __logger.warning('WLSDPLY-01678', schema_path, output_file_path, class_name=__class_name,
                         method_name=_method_name)
        return

    for item in model_list:
        if isinstance(item, dict):
            match = _find_object_match(item, output_list, schema_path)
            if match:
                next_schema_folder = wko_schema_helper.get_array_item_info(schema_folder)
                _update_dictionary(match, item, next_schema_folder, schema_path, output_file_path)
            else:
                output_list.append(item)
        elif item not in output_list:
            element_type = wko_schema_helper.get_array_element_type(schema_folder)
            item = _convert_value(item, element_type)
            output_list.append(item)


def _find_object_match(item, match_list, schema_path):
    """
    Find an object in match_list that has a name matching the item.
    :param item: the item to be matched
    :param match_list: a list of items
    :param schema_path: used for wko_schema_helper key lookup
    :return: a matching dictionary object
    """
    key = wko_schema_helper.get_object_list_key(schema_path)
    item_key = item[key]
    if item_key:
        for match_item in match_list:
            if isinstance(match_item, dict):
                if item_key == match_item[key]:
                    return match_item
    return None


def _convert_value(model_value, type_name):
    """
    Convert the specified model value to match the schema type for the domain resource file.
    WDT allows some model conventions that are not allowed in the domain resource file.
    :param model_value: the value to be checked
    :param type_name: the schema type name of the value
    :return: the converted value
    """
    if type_name == 'boolean':
        # the model values can be true, false, 1, 0, etc.
        # target boolean values must be 'true' or 'false'
        return PyRealBoolean(alias_utils.convert_boolean(model_value))

    if type_name == 'array':
        # the model values can be 'abc,123'.
        # target values must be a list object.
        return alias_utils.convert_to_type('list', model_value, delimiter=MODEL_LIST_DELIMITER)

    return model_value


# *** DELETE METHOD WHEN deprecated "named object list" IS REMOVED ***
def _check_named_object_list(model_value, type_name, schema_folder, schema_path, key):
    """
    Convert specified model value to an object list if it uses deprecated "named object list" format.
    :param model_value: the value to be checked
    :param type_name: the schema type name of the value
    :param schema_folder: the schema for the value being checked
    :param schema_path: used for wko_schema_helper key lookup
    :param key: used for wko_schema_helper key lookup
    :return: the converted value
    """
    if type_name == 'array' and isinstance(model_value, dict):
        object_list = list()
        next_schema_path = wko_schema_helper.append_path(schema_path, key)
        list_key = wko_schema_helper.get_object_list_key(next_schema_path)
        item_info = wko_schema_helper.get_array_item_info(schema_folder)
        properties = wko_schema_helper.get_properties(item_info)

        for model_key, model_object in model_value.items():
            new_object = model_object.copy()

            # see if the model name should become an attribute in the new object
            if (list_key in properties.keys()) and (list_key not in new_object.keys()):
                new_object[list_key] = model_key

            object_list.append(new_object)
        return object_list

    return model_value


def _get_or_create_dictionary(dictionary, key):
    if key not in dictionary:
        dictionary[key] = PyOrderedDict()

    return dictionary[key]
