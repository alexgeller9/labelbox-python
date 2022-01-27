from labelbox.schema.annotation_import import LabelImport
from labelbox.schema.labeling_frontend import LabelingFrontend
from labelbox.schema.ontology import Classification, OntologyBuilder, Option
import tensorflow_datasets as tfds
from PIL import Image
from io import BytesIO
from labelbox import Client
from tqdm import tqdm
import uuid
from scipy.io import loadmat
import os
import shutil
"""
# For final testing.
# Set this up to be N radio options
# Or N dropdowns. It doesn't really matter...
# We will just flatten it. Make sure both conditions work.
"""

if not os.path.exists('/tmp/miml'):
    if not shutil.which('7z'):
        raise Exception("Must have 7z installed")

    os.system(
        'wget http://www.lamda.nju.edu.cn/files/miml-image-data.rar -P /tmp/miml'
    )
    os.system('7z e /tmp/miml/miml-image-data.rar -o/tmp/miml/')
    os.system('7z e /tmp/miml/original.rar -o/tmp/miml/images')
    os.system('7z e /tmp/miml/processed.rar -o/tmp/miml/')

descriptions = ['desert', 'mountains', 'sea', 'sunset', 'trees']

client = Client()


def setup_project(client, class_names):
    project = client.create_project(name="multi_classification_image_project")
    dataset = client.create_dataset(name="multi_classification_image_dataset")
    ontology_builder = OntologyBuilder(classifications=[
        Classification(Classification.Type.CHECKLIST,
                       "description",
                       options=[Option(name) for name in class_names]),
    ])
    editor = next(
        client.get_labeling_frontends(where=LabelingFrontend.name == 'editor'))
    project.setup(editor, ontology_builder.asdict())
    project.datasets.connect(dataset)
    classification = project.ontology().classifications()[0]
    feature_schema_lookup = {
        'classification': classification.feature_schema_id,
        'options': {
            option.value: option.feature_schema_id
            for option in classification.options
        }
    }
    return project, dataset, feature_schema_lookup


annotations = []
max_examples = 350
ds = loadmat('/tmp/miml/miml data.mat')
class_names = [x[0][0] for x in ds['class_name']]
project, dataset, feature_schema_lookup = setup_project(client, class_names)
for example_idx in tqdm(range(20)):
    labels = [
        descriptions[i]
        for i in range(len(class_names))
        if ds['targets'][i, example_idx] > 0
    ]
    image_path = f"/tmp/miml/images/{1 + example_idx}.jpg"
    data_row = dataset.create_data_row(row_data=image_path)
    annotations.append({
        "uuid":
            str(uuid.uuid4()),
        "schemaId":
            feature_schema_lookup['classification'],
        "dataRow": {
            "id": data_row.uid
        },
        "answers": [{
            "schemaId": feature_schema_lookup['options'][class_name]
        } for class_name in labels]
    })
# Q are there any with None? If so we will handle in the ETL

print(f"Uploading {len(annotations)} annotations.")
job = LabelImport.create_from_objects(client, project.uid, str(uuid.uuid4()),
                                      annotations)
job.wait_until_done()
print("Upload Errors:", job.errors)
