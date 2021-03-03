import json
import logging
import time
from pathlib import Path
from typing import Any, List, NamedTuple, Optional
from typing import BinaryIO
from typing import Dict
from typing import Iterable
from typing import Set
from typing import Tuple
from typing import Union
from uuid import UUID, uuid4
from pydantic import BaseModel, validator
import pydantic
import backoff
import ndjson
from pydantic.types import conlist, constr
import requests
from typing_extensions import Annotated

from labelbox import utils
import labelbox.exceptions
from labelbox.orm import query
from labelbox.orm.db_object import DbObject
from labelbox.orm.model import Field
from labelbox.orm.model import Relationship
from labelbox.schema.enums import BulkImportRequestState
from pydantic import ValidationError
from typing import Set

try:
    from typing import TypedDict, Literal  # >=3.8
except ImportError:
    from typing_extensions import TypedDict, Literal


NDJSON_MIME_TYPE = "application/x-ndjson"
logger = logging.getLogger(__name__)



def _make_file_name(project_id: str, name: str) -> str:
    return f"{project_id}__{name}.ndjson"


# TODO(gszpak): move it to client.py
def _make_request_data(project_id: str, name: str, content_length: int,
                       file_name: str) -> dict:
    query_str = """mutation createBulkImportRequestFromFilePyApi(
            $projectId: ID!, $name: String!, $file: Upload!, $contentLength: Int!) {
        createBulkImportRequest(data: {
            projectId: $projectId,
            name: $name,
            filePayload: {
                file: $file,
                contentLength: $contentLength
            }
        }) {
            %s
        }
    }
    """ % query.results_query_part(BulkImportRequest)
    variables = {
        "projectId": project_id,
        "name": name,
        "file": None,
        "contentLength": content_length
    }
    operations = json.dumps({"variables": variables, "query": query_str})

    return {
        "operations": operations,
        "map": (None, json.dumps({file_name: ["variables.file"]}))
    }


# TODO(gszpak): move it to client.py
def _send_create_file_command(
        client, request_data: dict, file_name: str,
        file_data: Tuple[str, Union[bytes, BinaryIO], str]) -> dict:
    response = requests.post(
        client.endpoint,
        headers={"authorization": "Bearer %s" % client.api_key},
        data=request_data,
        files={file_name: file_data})

    try:
        response_json = response.json()
    except ValueError:
        raise labelbox.exceptions.LabelboxError(
            "Failed to parse response as JSON: %s" % response.text)

    response_data = response_json.get("data", None)
    if response_data is None:
        raise labelbox.exceptions.LabelboxError(
            "Failed to upload, message: %s" % response_json.get("errors", None))

    if not response_data.get("createBulkImportRequest", None):
        raise labelbox.exceptions.LabelboxError(
            "Failed to create BulkImportRequest, message: %s" %
            response_json.get("errors", None) or
            response_data.get("error", None))

    return response_data


class BulkImportRequest(DbObject):
    """Represents the import job when importing annotations.

    Attributes:
        name (str)
        state (Enum): FAILED, RUNNING, or FINISHED (Refers to the whole import job)
        input_file_url (str): URL to your web-hosted NDJSON file
        error_file_url (str): NDJSON that contains error messages for failed annotations
        status_file_url (str): NDJSON that contains status for each annotation
        created_at (datetime): UTC timestamp for date BulkImportRequest was created

        project (Relationship): `ToOne` relationship to Project
        created_by (Relationship): `ToOne` relationship to User
    """
    name = Field.String("name")
    state = Field.Enum(BulkImportRequestState, "state")
    input_file_url = Field.String("input_file_url")
    error_file_url = Field.String("error_file_url")
    status_file_url = Field.String("status_file_url")
    created_at = Field.DateTime("created_at")

    project = Relationship.ToOne("Project")
    created_by = Relationship.ToOne("User", False, "created_by")

    def refresh(self) -> None:
        """Synchronizes values of all fields with the database.
        """
        query_str, params = query.get_single(BulkImportRequest, self.uid)
        res = self.client.execute(query_str, params)
        res = res[utils.camel_case(BulkImportRequest.type_name())]
        self._set_field_values(res)

    def wait_until_done(self, sleep_time_seconds: int = 30) -> None:
        """Blocks import job until certain conditions are met.

        Blocks until the BulkImportRequest.state changes either to
        `BulkImportRequestState.FINISHED` or `BulkImportRequestState.FAILED`,
        periodically refreshing object's state.

        Args:
            sleep_time_seconds (str): a time to block between subsequent API calls
        """
        while self.state == BulkImportRequestState.RUNNING:
            logger.info(f"Sleeping for {sleep_time_seconds} seconds...")
            time.sleep(sleep_time_seconds)
            self.__exponential_backoff_refresh()

    @backoff.on_exception(
        backoff.expo,
        (labelbox.exceptions.ApiLimitError, labelbox.exceptions.TimeoutError,
         labelbox.exceptions.NetworkError),
        max_tries=10,
        jitter=None)
    def __exponential_backoff_refresh(self) -> None:
        self.refresh()

    @classmethod
    def from_name(cls, client, project_id: str,
                  name: str) -> 'BulkImportRequest':
        """ Fetches existing BulkImportRequest.

        Args:
            client (Client): a Labelbox client
            project_id (str): BulkImportRequest's project id
            name (str): name of BulkImportRequest
        Returns:
            BulkImportRequest object

        """
        query_str = """query getBulkImportRequestPyApi(
                $projectId: ID!, $name: String!) {
            bulkImportRequest(where: {
                projectId: $projectId,
                name: $name
            }) {
                %s
            }
        }
        """ % query.results_query_part(cls)
        params = {"projectId": project_id, "name": name}
        response = client.execute(query_str, params=params)
        return cls(client, response['bulkImportRequest'])

    @classmethod
    def create_from_url(cls, client, project_id: str, name: str,
                        url: str, validate = True) -> 'BulkImportRequest':
        """
        Creates a BulkImportRequest from a publicly accessible URL
        to an ndjson file with predictions.

        Args:
            client (Client): a Labelbox client
            project_id (str): id of project for which predictions will be imported
            name (str): name of BulkImportRequest
            url (str): publicly accessible URL pointing to ndjson file containing predictions
        Returns:
            BulkImportRequest object
        """
        if validate:
            logger.warn("Validation is turned on. The file will be downloaded locally and processed before uploading.")                
            res = requests.get(url)
            data = ndjson.loads(res.text)
            _validate_ndjson(data, client.get_project(project_id))
            

        query_str = """mutation createBulkImportRequestPyApi(
                $projectId: ID!, $name: String!, $fileUrl: String!) {
            createBulkImportRequest(data: {
                projectId: $projectId,
                name: $name,
                fileUrl: $fileUrl
            }) {
                %s
            }
        }
        """ % query.results_query_part(cls)
        params = {"projectId": project_id, "name": name, "fileUrl": url}
        bulk_import_request_response = client.execute(query_str, params=params)
        return cls(client,
                   bulk_import_request_response["createBulkImportRequest"])

    @classmethod
    def create_from_objects(cls, client, project_id: str, name: str,
                            predictions: Iterable[dict], validate = True) -> 'BulkImportRequest':
        """
        Creates a `BulkImportRequest` from an iterable of dictionaries.

        Conforms to JSON predictions format, e.g.:
        ``{
            "uuid": "9fd9a92e-2560-4e77-81d4-b2e955800092",
            "schemaId": "ckappz7d700gn0zbocmqkwd9i",
            "dataRow": {
                "id": "ck1s02fqxm8fi0757f0e6qtdc"
            },
            "bbox": {
                "top": 48,
                "left": 58,
                "height": 865,
                "width": 1512
            }
        }``

        Args:x
            client (Client): a Labelbox client
            project_id (str): id of project for which predictions will be imported
            name (str): name of BulkImportRequest
            predictions (Iterable[dict]): iterable of dictionaries representing predictions
        Returns:
            BulkImportRequest object
        """
        if validate:
            _validate_ndjson(predictions, client.get_project(project_id))

        data_str = ndjson.dumps(predictions)
        if not data_str:
            raise ValueError('annotations cannot be empty')

        data = data_str.encode('utf-8')
        file_name = _make_file_name(project_id, name)
        request_data = _make_request_data(project_id, name, len(data_str),
                                          file_name)
        file_data = (file_name, data, NDJSON_MIME_TYPE)
        response_data = _send_create_file_command(client,
                                                  request_data=request_data,
                                                  file_name=file_name,
                                                  file_data=file_data)

        return cls(client, response_data["createBulkImportRequest"])

    @classmethod
    def create_from_local_file(cls,
                               client,
                               project_id: str,
                               name: str,
                               file: Path,
                               validate_file=True) -> 'BulkImportRequest':
        """
        Creates a BulkImportRequest from a local ndjson file with predictions.

        Args:
            client (Client): a Labelbox client
            project_id (str): id of project for which predictions will be imported
            name (str): name of BulkImportRequest
            file (Path): local ndjson file with predictions
            validate_file (bool): a flag indicating if there should be a validation
                if `file` is a valid ndjson file
        Returns:
            BulkImportRequest object

        """
        file_name = _make_file_name(project_id, name)
        content_length = file.stat().st_size
        request_data = _make_request_data(project_id, name, content_length,
                                          file_name)

        with file.open('rb') as f:
            if validate_file:
                reader = ndjson.reader(f)
                # ensure that the underlying json load call is valid
                # https://github.com/rhgrant10/ndjson/blob/ff2f03c56b21f28f7271b27da35ca4a8bf9a05d0/ndjson/api.py#L53
                # by iterating through the file so we only store
                # each line in memory rather than the entire file
                try:
                    _validate_ndjson(reader, client.get_project(project_id))
                except ValueError:
                    raise ValueError(f"{file} is not a valid ndjson file")
                else:
                    f.seek(0)
            file_data = (file.name, f, NDJSON_MIME_TYPE)
            response_data = _send_create_file_command(client, request_data,
                                                      file_name, file_data)
        return cls(client, response_data["createBulkImportRequest"])



LabelboxID = constr(min_length=25, max_length=25, strict=True)

#TODO: These basic data types should be defined in a more central location
class Bbox(TypedDict):
    top: float
    left: float
    height: float
    width: float

class Point(TypedDict):
    x: float
    y: float

class VideoSupported(BaseModel):
    #Note that frames are only allowed as top level inferences for video
    frames : Optional[List[TypedDict("frames", {"end" : int, "start" : int})]]

class Builder:
    types : Set["BaseInference"]
    
    @classmethod
    def __get_validators__(cls):
        yield cls.validate
    
    @classmethod
    def validate(cls, data, values):
        top_level_fields = []
        max_match = 0
        matched = None
        
        for type_ in cls.types:
            determinate_fields = type_.Config.determinants(type_)
            top_level_fields.append(determinate_fields)
            matches = sum([val in determinate_fields for val in data])
            if matches == len(determinate_fields) and matches > max_match:
                max_match = matches
                matched = type_
            
        if matched is not None:
            if matched in [Radio, Text]:
                if isinstance(data['answer'], dict):
                    matched = Radio
                elif isinstance(data['answer'], str):
                    matched = Text
                else:
                    raise ValidationError(f"Unexpected type for answer. Found {data['answer']}. Expected a string or a dict")
            return matched(**data) 
        else:
            raise ValueError(f"Expected classes with values {data} to have keys matching one of the following : {top_level_fields}")


def _validate_ndjson(lines: Iterable[Dict[str, Any]], project) -> None:
    """     
    Notes:
        - Validation doesn't check data row data types. 
        This means we don't check to make sure that the annotation is valid for a particular data type.
        - video only supports radio and checklist tools and requires frame indices which we don't check for.
        - We also forbid extra so that might be too strict...
        - We also aren't checking bounds of the assets (eg frame index, image height, text length)
    """
    data_row_ids = {data_row.uid : data_row for dataset in project.datasets() for data_row in dataset.data_rows()}
    feature_schemas = get_valid_feature_schemas(project)
    uids = set()
    for idx, line in enumerate(lines):
        
        try:
            annotation = Annotation(value = line)
            annotation.validate(data_row_ids, feature_schemas)   
            uuid = annotation.value.uuid
            if uuid in uids:
                raise labelbox.exceptions.UuidError(
                    f'{uuid} already used in this import job, '
                    'must be unique for the project.')
            uids.add(uuid)
        except (ValidationError, ValueError) as e:
            raise labelbox.exceptions.NDJsonError(f"Invalid NDJson on line {idx}") from e
        


class BaseInference(BaseModel):
    ontology_type: str
    schemaId: LabelboxID
    uuid: UUID
    dataRow: TypedDict('dataRow' , {'id' : LabelboxID})

    def validate_datarow(self, valid_datarows):
        if self.dataRow['id'] not in valid_datarows:
            raise ValueError(f"datarow {self.dataRow['id']} is not attached to the specified project")

    def validate_feature_schemas(self, valid_feature_schemas):
        if self.schemaId not in valid_feature_schemas:
            raise ValueError(f"schema id {self.schemaId} is not valid for the provided project's ontology.")
        
        if self.ontology_type != valid_feature_schemas[self.schemaId]['tool']:
            raise ValueError(f"Schema id {self.schemaId} does not map to the assigned tool {valid_feature_schemas[self.schemaId]['tool']}")

        """
        child_schemas = valid_feature_schemas[self.value.schemaId].get('classifications',[])
        for subclass in getattr(self.value, 'classifications', []):
            if subclass.schemaId not in child_schemas:
                raise ValueError(f"Subclass does not have valid feature schema. Found {subclass.schemaId}. Expected on of {list(child_schemas.keys())}")
            subclass.validate_schema(subclass, child_schemas[subclass.schemaId])
        """

    class Config:
        #Users shouldn't to add extra data to the payload
        extra = 'forbid'
        @staticmethod
        def determinants(parent_cls) -> None:
            #This is a hack for better error messages
            return [k for k,v in parent_cls.__fields__.items() if 'determinant' in v.field_info.extra]

    
###Classifications:    
class Text(BaseInference):
    ontology_type: Literal["text"] = "text"
    answer: str = pydantic.Field(determinant = True)
    #No feature schema to check

class CheckList(VideoSupported, BaseInference):
    ontology_type: Literal["checklist"] = "checklist"
    answers: conlist(TypedDict('schemaId', {'schemaId': LabelboxID}), min_items = 1) = pydantic.Field(determinant = True)

    def validate_feature_schemas(self,  valid_feature_schemas):
        #Test top level feature schema for this tool
        super(CheckList, self).validate_feature_schemas(valid_feature_schemas)
        #Test the feature schemas provided to the answer field
        if len(set([answer['schemaId'] for answer in self.answers])) != len(self.answers):
            #TODO: Better message... :(
            raise ValueError("Checklist was provided more than one of the same labels.")
        for answer in self.answers:
            options = valid_feature_schemas[self.schemaId]['options']
            if answer['schemaId'] not in options:
                raise ValueError(f"Feature schema provided to {self.ontology_type} invalid. Expected on of {options}. Found {self.answer['schemaId']}")
    
class Radio(VideoSupported, BaseInference):
    ontology_type: Literal["radio"] = "radio"
    answer: TypedDict('schemaId' , {'schemaId': LabelboxID}) = pydantic.Field(determinant = True)

    def validate_feature_schemas(self, valid_feature_schemas):
        super(Radio, self).validate_feature_schemas(valid_feature_schemas)
        options = valid_feature_schemas[self.schemaId]['options']
        if self.answer['schemaId'] not in options:
            raise ValueError(f"Feature schema provided to {self.ontology_type} invalid. Expected on of {options}. Found {self.answer['schemaId']}")
    

class Classification(Builder):
    types = {Text,  CheckList, Radio}

#Localization - maps to tools in an ontology
class Localization(BaseInference):
    classifications : List[Classification] = []
    #This is indepdent of our problem
    def validate_feature_schemas(self, valid_feature_schemas):
        for classification in self.classifications:
            classification.validate_feature_schemas(valid_feature_schemas[self.schemaId]['classifications'])
    
    @validator('classifications', pre=True)
    def validate_subclasses(cls, value, field):
        #Create uuid and datarow id so we don't have to define classification objects twice
        #This is caused by the fact that we require these ids for top level classifications but not for subclasses
        results = []
        for row in value:
            row.update({'dataRow' : {'id' : 'child'.center(25, '_')}, 'uuid' : str(uuid4())})
            results.append(row)
        return results

class PolygonAnnotation(Localization):
    ontology_type: Literal["polygon"] = "polygon"
    polygon: List[Point] = pydantic.Field(determinant = True)

    @validator('polygon')
    def is_geom_valid(cls, v):
        if len(v) < 3:
            raise ValueError(f"A polygon must have at least 3 points to be valid. Found {v}")
        return v
    
class PolylineTool(Localization):
    ontology_type: Literal["line"] = "line"
    line: List[Point] = pydantic.Field(determinant = True)

    @validator('line')
    def is_geom_valid(cls, v):
        if len(v) < 2:
            raise ValueError(f"A line must have at least 2 points to be valid. Found {v}")
        return v
    
class RectangleTool(Localization):
    ontology_type: Literal["rectangle"] = "rectangle"
    bbox: Bbox = pydantic.Field(determinant = True)
    #Could check if points are positive

class PointTool(Localization):
    ontology_type: Literal["point"]    = "point" 
    point: Point = pydantic.Field(determinant = True)
    #Could check if points are positive

class EntityTool(Localization):
    ontology_type: Literal["named-entity"]     = "named-entity"    
    location : TypedDict("TextLocation" , {'start' : int, 'end' : int}) = pydantic.Field(determinant = True)

    @validator('location')
    def is_valid_location(cls, v):
        if len(v) < 2:
            raise ValueError(f"A line must have at least 2 points to be valid. Found {v}")
        if v['start'] < 0:
            raise ValueError(f"Text location must be positive. Found {v}")
        if v['start'] >= v['end']:
            raise ValueError(f"Text start location must be less than end. Found {v}")
        return v

class MaskPrediction(Localization):
    ontology_type: Literal["superpixel"] = "superpixel"
    mask : TypedDict("mask" , {'instanceURI' : constr(min_length = 5, strict = True), 'colorRGB' : Tuple[int,int,int]}) = pydantic.Field(determinant = True)
    @validator('mask')
    def is_valid_mask(cls, v):
        colors = v['colorRGB']
            #Does the dtype matter? Can it be a float?
        if not isinstance(colors, (tuple, list)):
            raise ValueError(f"Received color that is not a list or tuple. Found : {colors}")
        elif len(colors) != 3:
            raise ValueError(f"Must provide RGB values for segmentation colors. Found : {colors}")
        elif not all([0 <= color <= 255 for color in colors]):
            raise ValueError(f"All rgb colors must be between 0 and 255. Found : {colors}")
        return v

class Tool(Builder):
    types = {MaskPrediction,  EntityTool ,PointTool, RectangleTool, PolylineTool , PolygonAnnotation, *Classification.types}




#TODO: What does the error message look like?
#Do both conditions bark if one of them fails? #Or does that only happen until it is able to first successfully construct an obj

class Annotation(BaseModel):
    value: Union[Tool, Classification]

    def validate(self, valid_datarows, valid_feature_schemas):
        self.value.validate_feature_schemas(valid_feature_schemas)
        self.value.validate_datarow(valid_datarows)
    
    """
    
    def validate_feature_schema(self, valid_feature_schemas):
        #We also want to check if the feature schema matches the tool...
        
        
        self.validate_classification(self.value, valid_feature_schemas[self.schemaId])

        #TODO: When ontology management is complete we should change this schema checking with the proper objects
        child_schemas = valid_feature_schemas[self.value.schemaId].get('classifications',[])
        for subclass in getattr(self.value, 'classifications', []):
            if subclass.schemaId not in child_schemas:
                raise ValueError(f"Subclass does not have valid feature schema. Found {subclass.schemaId}. Expected on of {list(child_schemas.keys())}")
            self.validate_classification(subclass, child_schemas[subclass.schemaId])

    def validate_classification(self,  classification, feature_schema):
        answers = []
        if isinstance(classification, CheckList):
            answers = classification.answers
            if len(set([answer['schemaId'] for answer in answers])) != len(answers):
                #TODO: Better message... :(
                raise ValueError("Checklist was provided more than one of the same labels.")
        elif isinstance(classification, Radio):    
            answers = [classification.answer]
        for answer in answers:
            options = feature_schema['options']
            if answer['schemaId'] not in options:
                raise ValueError(f"Feature schema provided to {classification.ontology_type} invalid. Expected on of {options}. Found {answer['schemaId']}")
    
    def validate(self, valid_datarows, valid_feature_schemas):
        self.validate_feature_schema(valid_feature_schemas)
        self.validate_datarow(valid_datarows)
    """

def parse_classification(tool):
    """
    Only radio, checklist, and text are supported for mal
    """
    if tool['type'] in ['radio', 'checklist']:
        return {'tool' : tool['type'], 'featureSchemaId' : tool['featureSchemaId'] , 'options' : [r['featureSchemaId'] for r in tool['options']]}
    elif tool['type'] == 'text':
        return {'tool' : tool['type'],  'featureSchemaId' : tool['featureSchemaId']}
    #Other subtypes not supported
    
def get_valid_feature_schemas(project):
    ontology = project.ontology()
    #print(ontology)
    valid_feature_schemas = {}
    for tool in ontology.normalized['tools']:
        classifications = [parse_classification(classification_tool) for classification_tool in tool['classifications']]
        classifications = {v['featureSchemaId'] : v for v in classifications}
        valid_feature_schemas[tool['featureSchemaId']] = {'tool' : tool['tool'], 'classifications' : classifications}
    for tool in ontology.normalized['classifications']:
        valid_feature_schemas[tool['featureSchemaId']] = parse_classification(tool)
    return valid_feature_schemas
    
