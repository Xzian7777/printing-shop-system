"""Catalog validation used before shared Firestore writes."""
import math


def validate_service(data):
    name = str(data.get('name') or '').strip()
    key = str(data.get('id') or '').strip()
    if not name or len(name) > 500 or not key or '/' in key or len(key) > 100:
        raise ValueError('Provide a service name and a valid ID (max 100 characters, without /).')
    price = float(data.get('basePrice', 0))
    sort = int(data.get('sortOrder', 1))
    if not math.isfinite(price) or not 0 <= price <= 5000000 or not 0 <= sort <= 100000:
        raise ValueError('Invalid base price or display order.')
    requirements, labels = [], set()
    for raw in data.get('requirements') or []:
        label = str(raw.get('label') or '').strip()
        kind = raw.get('type', 'text')
        if not label or len(label) > 150 or label in labels or kind not in ('text', 'number', 'select', 'radio', 'checkboxes'):
            raise ValueError('Requirements must have unique labels and supported types.')
        labels.add(label)
        options = raw.get('options') or []
        if isinstance(options, str): options = options.replace(',', '\n').splitlines()
        options = list(dict.fromkeys(str(option).strip() for option in options if str(option).strip()))
        if kind in ('select', 'radio', 'checkboxes') and not options:
            raise ValueError(f'Provide choices for {label}.')
        requirements.append(dict(raw, label=label, type=kind, options=options, required=raw.get('required') is not False))
    return dict(data, id=key, name=name, basePrice=price, sortOrder=sort, requirements=requirements)


def validate_requirements(service, supplied):
    if not isinstance(supplied, dict): raise ValueError('Invalid service requirements.')
    for req in service.get('requirements') or []:
        label = str(req.get('label') or req.get('name') or '')
        value = supplied.get(label)
        if req.get('required') is not False and (value is None or value == [] or not str(value).strip()):
            raise ValueError(f'Please provide: {label}')
        if value is None or value == '' or value == []: continue
        if req.get('type') == 'number':
            text = str(value)
            if not text.isascii() or not text.isdigit() or not 1 <= int(text) <= 100000:
                raise ValueError(f'{label} must be a whole number from 1 to 100,000.')
        elif req.get('type') in ('select', 'radio', 'checkboxes'):
            options = req.get('options') or []
            if isinstance(options, str): options = [o.strip() for o in options.replace('/', ',').split(',')]
            values = value if isinstance(value, list) else [value]
            if any(v not in options for v in values): raise ValueError(f'Invalid choice for {label}.')
        elif len(str(value)) > 150:
            raise ValueError(f'{label} cannot exceed 150 characters.')
