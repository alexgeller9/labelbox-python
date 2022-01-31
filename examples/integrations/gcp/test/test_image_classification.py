import requests
import hmac
import hashlib
import json
"""
This script simulates a webhook event.

# Make sure that the LABELBOX_API_KEY cooresponds to the org that belongs to this project.
# If you want any real data to be produced, there should be some bounding boes in the project.
"""
project_id = "ckyx33mocbagp0zb2d14b7ma7"
secret = b'test_secret'

payload = json.dumps({
    'project_id': project_id,
    'pipeline': 'image_single_classification'
})
signature = "sha1=" + hmac.new(
    secret, msg=payload.encode(), digestmod=hashlib.sha1).hexdigest()
res = requests.post("http://localhost:8000/project",
                    data=payload,
                    headers={'X-Hub-Signature': signature})
