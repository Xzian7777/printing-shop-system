with open('app.py', 'r', encoding='utf-8') as f:
    app_code = f.read()

# 1. Update imports from firebase_config
app_code = app_code.replace(
    '    get_services, save_service, delete_service,',
    '    get_services, save_service, delete_service,\n    archive_item, get_archived_items, restore_archived_item, permanently_delete_archived_item,'
)

# 2. Update User DELETE endpoint to archive user data before deleting
old_user_delete = '''    elif request.method == 'DELETE':
        # DELETE USER (ADMIN CRUD)
        try:
            deleted = False
            
            # 1. Subukang burahin gamit ang Firebase Realtime Database reference kung naka-configure
            try:
                user_ref = db.reference(f'customers/{user_id}')
                if user_ref.get():
                    user_ref.delete()
                    deleted = True
            except Exception:
                pass

            # 2. Subukang burahin kung Firestore ang ginagamit
            try:
                if db:
                    doc_ref = db.collection('customers').document(user_id)
                    if doc_ref.get().exists:
                        doc_ref.delete()
                        deleted = True
                    else:
                        customers_ref = db.collection('customers')
                        docs = customers_ref.where('id', '==', user_id).stream()
                        for doc in docs:
                            doc.reference.delete()
                            deleted = True
            except Exception as db_err:
                print(f"? Database User Delete Error: {db_err}")

            return jsonify({"success": True, "status": "success", "message": f"User {user_id} successfully deleted from database."}), 200'''

new_user_delete = '''    elif request.method == 'DELETE':
        # ARCHIVE USER INSTEAD OF HARD DELETION (ADMIN CRUD)
        try:
            user_data = None
            try:
                if db:
                    doc_ref = db.collection('customers').document(user_id)
                    snap = doc_ref.get()
                    if snap.exists:
                        user_data = snap.to_dict()
                    else:
                        docs = db.collection('customers').where('id', '==', user_id).stream()
                        for doc in docs:
                            user_data = doc.to_dict()
                            break
            except Exception as e:
                print(f"Error fetching user for archive: {e}")

            if not user_data:
                user_data = {'id': user_id, 'name': user_id, 'email': ''}

            # Archive the user
            archive_item('user', user_id, user_data, 'admin_users')

            # Delete from active customers collection
            try:
                if db:
                    db.collection('customers').document(user_id).delete()
                    docs = db.collection('customers').where('id', '==', user_id).stream()
                    for doc in docs:
                        doc.reference.delete()
            except Exception as db_err:
                print(f"User delete error after archive: {db_err}")

            return jsonify({"success": True, "status": "success", "message": f"User {user_id} has been moved to Archive successfully."}), 200'''

app_code = app_code.replace(old_user_delete, new_user_delete)

# 3. Update Sample DELETE endpoint to archive sample
old_sample_delete = '''        if request.method == 'DELETE':
            success = delete_sample_product(sample_id)
            if success:
                return jsonify({'success': True, 'message': 'Sample successfully deleted.'}), 200
            return jsonify({'success': False, 'message': 'Failed to delete sample.'}), 500'''

new_sample_delete = '''        if request.method == 'DELETE':
            # Retrieve sample data for archive
            sample_data = None
            try:
                samples = get_sample_products() or []
                for s in samples:
                    if str(s.get('id')) == str(sample_id):
                        sample_data = s
                        break
            except Exception:
                pass
            if not sample_data:
                sample_data = {'id': sample_id, 'title': sample_id}

            archive_item('sample_product', sample_id, sample_data, 'admin_samples')
            delete_sample_product(sample_id)
            return jsonify({'success': True, 'message': 'Sample moved to Archive successfully.'}), 200'''

app_code = app_code.replace(old_sample_delete, new_sample_delete)

# 4. Update Service DELETE endpoint to archive service
old_service_delete = '''        if request.method == 'DELETE':
            success = delete_service(service_id)
            if success:
                return jsonify({'success': True, 'message': 'Service successfully deleted.'}), 200
            return jsonify({'success': False, 'message': 'Failed to delete service.'}), 500'''

new_service_delete = '''        if request.method == 'DELETE':
            service_data = None
            try:
                services = get_services() or []
                for s in services:
                    if str(s.get('id')) == str(service_id):
                        service_data = s
                        break
            except Exception:
                pass
            if not service_data:
                service_data = {'id': service_id, 'name': service_id}

            archive_item('service', service_id, service_data, 'admin_services')
            delete_service(service_id)
            return jsonify({'success': True, 'message': 'Printing service moved to Archive successfully.'}), 200'''

app_code = app_code.replace(old_service_delete, new_service_delete)

# 5. Add Archive API Endpoints and Store Product Archive Endpoint
archive_endpoints = '''
# ---------------------------------------------------------------------------
# ARCHIVE MANAGEMENT ENDPOINTS
# ---------------------------------------------------------------------------

@app.route('/api/archive', methods=['GET'])
def get_archives():
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'message': 'Unauthorized! Admin access required.'}), 403
    try:
        item_type = request.args.get('type', 'all')
        archives = get_archived_items(item_type)
        return jsonify({'success': True, 'archives': archives}), 200
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/archive/<archive_id>/restore', methods=['POST'])
def restore_archive(archive_id):
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'message': 'Unauthorized! Admin access required.'}), 403
    try:
        success, message = restore_archived_item(archive_id)
        return jsonify({'success': success, 'message': message}), 200 if success else 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/archive/<archive_id>', methods=['DELETE'])
def delete_archive_permanent(archive_id):
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'message': 'Unauthorized! Admin access required.'}), 403
    try:
        success = permanently_delete_archived_item(archive_id)
        return jsonify({'success': success, 'message': 'Item permanently deleted from archive.'}), 200 if success else 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/store/products/<product_id>', methods=['DELETE'])
def delete_store_product_endpoint(product_id):
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'message': 'Unauthorized! Admin access required.'}), 403
    try:
        current_products = get_store_products()
        target_prod = None
        new_list = []
        for p in current_products:
            if str(p.get('id')) == str(product_id):
                target_prod = p
            else:
                new_list.append(p)
        
        if target_prod:
            archive_item('store_product', product_id, target_prod, 'admin_store')
        save_store_products(new_list)
        return jsonify({'success': True, 'message': 'In-store product moved to Archive successfully.'}), 200
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
'''

if '/api/archive' not in app_code:
    app_code += archive_endpoints

with open('app.py', 'w', encoding='utf-8') as f:
    f.write(app_code)

print('app.py updated with archive endpoints and deletion handlers')
