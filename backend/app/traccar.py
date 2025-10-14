import os
import requests

def _get_traccar_url():
    return os.environ.get("TRACCAR_URL") or "http://gps.frotaweb.com"

def get(path, headers=None, params=None):
    return requests.get(f"{_get_traccar_url().rstrip('/')}/{path.lstrip('/')}", headers=headers, params=params)

def post(self, path, json):
    return requests.post(f"{_get_traccar_url().rstrip('/')}/{path.lstrip('/')}", json=json)

def put(self, path, json):
    return requests.put(f"{_get_traccar_url().rstrip('/')}/{path.lstrip('/')}", json=json)

def delete(self, path):
    return requests.delete(f"{_get_traccar_url().rstrip('/')}/{path.lstrip('/')}")
