"""Firebase Admin SDK initialization and Firestore connection."""
import firebase_admin
from firebase_admin import credentials, firestore

try:
    if not firebase_admin._apps:
        cred = credentials.Certificate("firebase-service-account.json")
        firebase_admin.initialize_app(cred)
    db = firestore.client()
except Exception as error:
    print(f"Error initializing Firebase: {error}")
    db = None
