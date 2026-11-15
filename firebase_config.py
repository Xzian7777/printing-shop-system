"""Firebase Admin SDK initialization and Firestore connection."""
import firebase_admin
import json
import os
from firebase_admin import credentials, firestore
from dotenv import load_dotenv

load_dotenv()

try:
    if not firebase_admin._apps:
        account_json = os.getenv('FIREBASE_SERVICE_ACCOUNT_JSON')
        cred = credentials.Certificate(json.loads(account_json) if account_json else "firebase-service-account.json")
        firebase_admin.initialize_app(cred)
    db = firestore.client()
except Exception as error:
    print(f"Error initializing Firebase: {type(error).__name__}")
    db = None
