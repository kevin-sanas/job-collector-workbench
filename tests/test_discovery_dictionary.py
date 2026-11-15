import json
import tempfile
import unittest
from pathlib import Path

from app.discovery.dictionary import build_alias_index, load_dictionary


class DiscoveryDictionaryTests(unittest.TestCase):
    def write_dictionary(self, data: dict) -> Path:
        temp_dir = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(temp_dir.cleanup)
        path = Path(temp_dir.name) / 'dictionary.json'
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        return path

    def valid_data(self) -> dict:
        return {
            'dictionary_version': 'tech-dict.test.1',
            'terms': [
                {
                    'tech_key': 'python',
                    'display_name': 'Python',
                    'aliases': ['Python', 'PYTHON'],
                    'category': '语言',
                    'enabled': True,
                    'match_mode': 'token',
                },
                {
                    'tech_key': 'spring-boot',
                    'display_name': 'Spring Boot',
                    'aliases': ['Spring Boot', 'SpringBoot'],
                    'category': '框架',
                    'enabled': True,
                    'match_mode': 'phrase',
                },
                {
                    'tech_key': 'artificial-intelligence',
                    'display_name': '人工智能',
                    'aliases': ['AI'],
                    'category': '方向',
                    'enabled': True,
                    'match_mode': 'review',
                },
                {
                    'tech_key': 'disabled-term',
                    'display_name': '禁用词',
                    'aliases': ['Disabled'],
                    'category': '测试',
                    'enabled': False,
                    'match_mode': 'token',
                },
            ],
        }

    def test_loads_modes_and_builds_casefolded_enabled_alias_index(self):
        dictionary = load_dictionary(self.write_dictionary(self.valid_data()))
        aliases = build_alias_index(dictionary)

        self.assertEqual(dictionary.dictionary_version, 'tech-dict.test.1')
        self.assertEqual(len(dictionary.terms), 4)
        by_alias = {item.normalized_alias: item for item in aliases}
        self.assertEqual(by_alias['python'].tech_key, 'python')
        self.assertEqual(by_alias['spring boot'].match_mode, 'phrase')
        self.assertTrue(by_alias['ai'].review_required)
        self.assertNotIn('disabled', by_alias)
        self.assertEqual(sum(item.normalized_alias == 'python' for item in aliases), 1)

    def test_rejects_missing_fields_invalid_keys_and_empty_aliases(self):
        cases = []
        missing_version = self.valid_data()
        del missing_version['dictionary_version']
        cases.append(missing_version)
        invalid_key = self.valid_data()
        invalid_key['terms'][0]['tech_key'] = 'Python!'
        cases.append(invalid_key)
        empty_alias = self.valid_data()
        empty_alias['terms'][0]['aliases'] = ['  ']
        cases.append(empty_alias)
        invalid_mode = self.valid_data()
        invalid_mode['terms'][0]['match_mode'] = 'substring'
        cases.append(invalid_mode)

        for data in cases:
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    load_dictionary(self.write_dictionary(data))

    def test_rejects_same_enabled_alias_owned_by_different_terms(self):
        data = self.valid_data()
        data['terms'].append({
            'tech_key': 'python-other',
            'display_name': 'Other Python',
            'aliases': [' python '],
            'category': '测试',
            'enabled': True,
            'match_mode': 'token',
        })

        with self.assertRaisesRegex(ValueError, '别名.*python'):
            load_dictionary(self.write_dictionary(data))

    def test_rejects_non_string_dictionary_text_fields(self):
        cases = []
        numeric_version = self.valid_data()
        numeric_version['dictionary_version'] = 42
        cases.append(numeric_version)
        boolean_alias = self.valid_data()
        boolean_alias['terms'][0]['aliases'] = [True]
        cases.append(boolean_alias)
        list_name = self.valid_data()
        list_name['terms'][0]['display_name'] = ['Python']
        cases.append(list_name)
        object_category = self.valid_data()
        object_category['terms'][0]['category'] = {'name': '语言'}
        cases.append(object_category)

        for data in cases:
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    load_dictionary(self.write_dictionary(data))

    def test_bundled_dictionary_is_explicitly_draft_and_loadable(self):
        path = Path(__file__).resolve().parents[1] / 'config' / 'discovery' / 'tech_dictionary.json'

        dictionary = load_dictionary(path)

        self.assertTrue(dictionary.dictionary_version.startswith('tech-dict.draft-'))
        self.assertGreaterEqual(len(dictionary.terms), 20)
        self.assertLessEqual(len(dictionary.terms), 1000)
        self.assertTrue({'spring-cloud', 'sql-server', 'rag'}.issubset({term.tech_key for term in dictionary.terms}))


if __name__ == '__main__':
    unittest.main()
