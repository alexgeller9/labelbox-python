import base64
from io import BytesIO

from google.cloud import aiplatform
from PIL import Image, ImageDraw, ImageDraw, ImageFont

endpoint_name = "image_single_classification_2022-02-15_00:22:02.965687_endpoint"
endpoint = aiplatform.Endpoint.list(filter=f'display_name="{endpoint_name}"')[0]

with open('dog.jpg', "rb") as f:
    image_bytes = f.read()

# Note that the max content size is 1.5mb
b64_bytes = base64.b64encode(image_bytes).decode("utf-8")
result = endpoint.predict(instances=[{
    'content': b64_bytes
}],
                          parameters={
                              'confidenceThreshold': 0.5,
                              'maxPredictions': 5
                          })
print(result)

im = Image.open(BytesIO(image_bytes))
w, h = im.size
d = ImageDraw.Draw(im)
for prediction in result.predictions:
    for name, confidence in zip(prediction['displayNames'],
                                prediction['confidences']):
        d.text((10, 10),
               f"{name} : {round(confidence*100, 2)}%",
               fill=(255, 255, 255))
im.show()
