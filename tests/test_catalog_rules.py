import unittest
from catalog_rules import validate_service, validate_requirements


class CatalogRulesTests(unittest.TestCase):
    def test_service_choices_and_required_flags_are_preserved(self):
        service = validate_service({'id': 'custom', 'name': 'Custom', 'basePrice': 25, 'sortOrder': 1,
                                    'requirements': [{'label': 'Finish', 'type': 'select', 'options': ['Gloss', 'Matte']}]})
        self.assertEqual(service['basePrice'], 25)
        self.assertTrue(service['requirements'][0]['required'])
        validate_requirements(service, {'Finish': 'Gloss'})
        with self.assertRaises(ValueError): validate_requirements(service, {})
        with self.assertRaises(ValueError): validate_requirements(service, {'Finish': 'Unknown'})

    def test_checkbox_and_radio_selections_are_validated(self):
        service = {'requirements': [{'label': 'Extras', 'type': 'checkboxes', 'required': True, 'options': ['Cut', 'Fold']}]}
        validate_requirements(service, {'Extras': ['Cut', 'Fold']})
        with self.assertRaises(ValueError): validate_requirements(service, {'Extras': []})
        with self.assertRaises(ValueError): validate_requirements(service, {'Extras': ['Unknown']})

    def test_invalid_service_prices_and_duplicate_labels_are_rejected(self):
        for price in [-1, float('inf'), float('nan'), 5000001]:
            with self.assertRaises(ValueError): validate_service({'id': 'a', 'name': 'A', 'basePrice': price})
        with self.assertRaises(ValueError): validate_service({'id': 'a', 'name': 'A', 'requirements': [{'label': 'X'}, {'label': 'X'}]})

    def test_custom_number_limits_and_optional_text(self):
        service = {'requirements': [{'label': 'Quantity', 'type': 'number'}, {'label': 'Note', 'required': False}]}
        validate_requirements(service, {'Quantity': '100000'})
        for value in ['0', '-1', '100001', '1.5', '9' * 100, 'abc']:
            with self.assertRaises(ValueError): validate_requirements(service, {'Quantity': value})


if __name__ == '__main__': unittest.main()
