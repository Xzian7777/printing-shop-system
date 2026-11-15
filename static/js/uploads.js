window.ShopUploads = {
    async request(path, data) {
        const response = await fetch(path, {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)
        });
        let result;
        try { result = await response.json(); }
        catch (_) { throw Error('The shop server returned an invalid upload response. Please retry.'); }
        if (!response.ok || !result.success) throw Error(result.message || 'Unable to upload the file.');
        return result;
    },
    async upload(file, purpose = 'document', onProgress = () => {}) {
        if (!file.size || file.size > 10 * 1024 * 1024) throw Error('Each file must be between 1 byte and 10 MB.');
        const submissionId = crypto.randomUUID();
        const prepared = await this.request('/api/uploads/prepare', {
            submissionId, purpose, files: [{name: file.name, size: file.size}]
        });
        const ticket = prepared.files[0];
        if (ticket.result) return ticket.result;
        await new Promise((resolve, reject) => {
            const xhr = new XMLHttpRequest();
            xhr.open('PUT', ticket.uploadUrl);
            xhr.timeout = 120000;
            xhr.upload.onprogress = event => {
                if (event.lengthComputable) onProgress(Math.round(event.loaded / event.total * 90));
            };
            xhr.onload = () => xhr.status >= 200 && xhr.status < 300
                ? resolve() : reject(Error('Cloud storage upload failed. Please retry.'));
            xhr.onerror = xhr.ontimeout = () => reject(Error('The upload connection was interrupted. Please retry.'));
            const form = new FormData();
            form.append('file', file, file.name);
            xhr.send(form);
        });
        let result;
        for (let attempt = 0; attempt < 3; attempt++) {
            try { result = await this.request('/api/uploads/finalize', {fileId: ticket.fileId}); break; }
            catch (error) {
                if (attempt === 2) throw error;
                await new Promise(resolve => setTimeout(resolve, 1500));
            }
        }
        onProgress(100);
        return result;
    }
};
