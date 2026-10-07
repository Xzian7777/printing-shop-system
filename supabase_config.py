import os
import datetime
from supabase import create_client, Client

# ===========================================================================
# SUPABASE CREDENTIALS (Palitan ng totoong values mula sa Supabase Dashboard)
# ===========================================================================
SUPABASE_URL = "https://ymtngztgkrnxfayvavoe.supabase.co/rest/v1/"       # Palitan ng Project URL mo
SUPABASE_KEY = "sb_publishable_rrZtHSRZZSSq4pCXUCZhfw_aLHISE51"        # Palitan ng anon/public key mo
BUCKET_NAME = "kezcemzek-files"                                # Palitan ng pangalan ng Storage Bucket mo

# Initialize Supabase Client
try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    print("✅ Supabase Client Successfully Initialized!")
except Exception as e:
    print(f"❌ Error initializing Supabase: {e}")
    supabase = None


def upload_file_to_supabase(file_obj, filename=None):
    """
    I-o-upload ang file object (mula sa Flask request.files) sa Supabase Storage
    at ibabalik ang Public Direct URL nito.
    """
    try:
        if not supabase:
            print("⚠ Hindi naka-initialize ang Supabase client.")
            return None

        original_name = filename or file_obj.filename
        
        # Gumawa ng unique filename gamit ang timestamp para maiwasan ang duplicate overwrite
        timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        safe_filename = f"{timestamp}_{original_name.replace(' ', '_')}"
        storage_path = f"uploads/{safe_filename}"

        # Basahin ang file content
        file_bytes = file_obj.read()
        file_obj.seek(0)  # I-reset ang pointer sa umpisa

        # I-upload sa Supabase Bucket
        supabase.storage.from_(BUCKET_NAME).upload(
            path=storage_path,
            file=file_bytes,
            file_options={"content-type": file_obj.content_type or "application/octet-stream"}
        )

        # Kunin ang Direct Public URL para mabuksan/ma-download agad ng Admin
        public_url = supabase.storage.from_(BUCKET_NAME).get_public_url(storage_path)
        print(f"✅ File uploaded to Supabase Storage: {public_url}")
        return public_url

    except Exception as e:
        print(f"❌ Supabase Upload Error: {e}")
        return None