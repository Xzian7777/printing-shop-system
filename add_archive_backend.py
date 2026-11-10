with open('app.py', 'r', encoding='utf-8') as f:
    code = f.read()

archive_code = '''

# ---------------------------------------------------------------------------
# ARCHIVE MANAGEMENT SYSTEM (Soft-delete & Restore for Users, Services, Products, Samples)
# ---------------------------------------------------------------------------

def archive_item(item_type, item_id, item_data=None, original_source=None):
    """
    Archives an item into the Firestore archives collection.
    item_type can be: user, service, store_product, sample_product
    """
    try:
        if db is not None:
            import datetime
            archived_id = f"{item_type}_{item_id}"
            payload = {
                'id': archived_id,
                'originalId': str(item_id),
                'itemType': item_type,
                'data': item_data or {},
                'source': original_source or 'admin',
                'archivedAt': datetime.datetime.now().isoformat()
            }
            db.collection('archives').document(archived_id).set(payload, merge=True)
            print(f"✓ Archived {item_type} ({item_id}) successfully!")
            return True
        return False
    except Exception as e:
        print(f"Error archiving item: {e}")
        return False

def get_archived_items(item_type=None):
    """
    Retrieves archived items from Firestore archives collection.
    """
    try:
        if db is not None:
            col = db.collection('archives')
            if item_type and item_type != 'all':
                docs = col.where('itemType', '==', item_type).stream()
            else:
                docs = col.stream()
            items = []
            for d in docs:
                data = d.to_dict()
                data['archiveDocId'] = d.id
                items.append(data)
            items.sort(key=lambda x: x.get('archivedAt', ''), reverse=True)
            return items
        return []
    except Exception as e:
        print(f"Error fetching archives: {e}")
        return []

def restore_archived_item(archive_doc_id):
    """
    Restores an archived item back to its original collection and deletes it from archives.
    """
    try:
        if db is not None:
            doc_ref = db.collection('archives').document(archive_doc_id)
            doc = doc_ref.get()
            if not doc.exists:
                return False, "Archived item not found."
            
            item = doc.to_dict()
            item_type = item.get('itemType')
            orig_id = item.get('originalId')
            data = item.get('data') or {}

            if item_type == 'user':
                db.collection('customers').document(str(orig_id)).set(data, merge=True)
            elif item_type == 'service':
                save_service(data)
            elif item_type == 'store_product':
                current_products = get_store_products()
                exists = any(str(p.get('id')) == str(orig_id) for p in current_products)
                if not exists:
                    current_products.append(data)
                    save_store_products(current_products)
            elif item_type == 'sample_product':
                save_sample_product(data)
            else:
                return False, f"Unknown item type: {item_type}"

            doc_ref.delete()
            return True, f"Item ({orig_id}) restored successfully!"
        return False, "Database not connected."
    except Exception as e:
        print(f"Error restoring archived item: {e}")
        return False, str(e)

def permanently_delete_archived_item(archive_doc_id):
    """
    Permanently deletes an item from the archives collection.
    """
    try:
        if db is not None:
            db.collection('archives').document(archive_doc_id).delete()
            return True
        return False
    except Exception as e:
        print(f"Error permanently deleting archive: {e}")
        return False
'''

if 'ARCHIVE MANAGEMENT SYSTEM' not in code:
    code += archive_code
    with open('app.py', 'w', encoding='utf-8') as f:
        f.write(code)
    print('app.py updated with archive functions')
else:
    print('Already present in app.py')
