"""Production WSGI entry point."""
import os

os.environ.setdefault('APP_ENV', 'production')
from app import app
