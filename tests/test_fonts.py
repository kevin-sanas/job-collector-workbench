import unittest
from app.fonts import accept_digit_matches, DisplayFontDecoder


class FontTests(unittest.TestCase):
    def test_decoder_passes_only_supported_primitive_js_arguments(self):
        class Page:
            tab_id = 'one'
            url = 'https://www.zhipin.com/'
            def run_js(self, script, selector, codes):
                if not isinstance(codes, str):
                    raise TypeError('DrissionPage 4.1 does not accept a list')
                return {'57394': [{'digit': '1', 'score': .99}, {'digit': '7', 'score': .4}]}
        self.assertEqual(DisplayFontDecoder('.salary').decode(Page(), '\ue0320K'), '10K')

    def test_accepts_only_confident_distinct_glyph_matches(self):
        self.assertEqual(accept_digit_matches({'57393': [{'digit': '7', 'score': .99}, {'digit': '1', 'score': .4}]}), {57393: '7'})

    def test_rejects_weak_ambiguous_and_duplicate_matches(self):
        self.assertEqual(accept_digit_matches({'1': [{'digit': '7', 'score': .7}, {'digit': '1', 'score': .4}]}), {})
        self.assertEqual(accept_digit_matches({'1': [{'digit': '7', 'score': .99}, {'digit': '1', 'score': .98}]}), {})
        self.assertEqual(accept_digit_matches({'1': [{'digit': '7', 'score': .99}, {'digit': '1', 'score': .4}], '2': [{'digit': '7', 'score': .99}, {'digit': '1', 'score': .4}]}), {})
