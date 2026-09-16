"""Mangum adapter so the FastAPI app in src/omnidamus/web.py can run on AWS Lambda."""

from mangum import Mangum

from omnidamus.web import app

handler = Mangum(app)
