import os

import httpx
from dotenv import load_dotenv

load_dotenv() 
key = os.environ["AMAP_WEB_KEY"]  

response = httpx.get(
    "https://restapi.amap.com/v3/geocode/geo",
    params={"key": key, "address": "北京大学"},
)
data = response.json()
print(data["geocodes"][0]["location"])