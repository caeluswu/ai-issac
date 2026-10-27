import unittest
from mechanisms.js_patch import apply_patch,function_span


class PatchTests(unittest.TestCase):
    def test_nested_braces_and_strings_are_preserved(self):
        source='function init(){return {x:"}"};}\nfunction proposeBatch(c){/* } */if(c){return {x:"{"};}return [];}'
        replacement='function proposeBatch(c){return [{x:c?1:0}];}'
        result=apply_patch(source,'proposeBatch',replacement)
        self.assertIn('function init(){return {x:"}"};}',result)
        self.assertIn(replacement,result)

    def test_duplicate_and_extra_source_fail(self):
        with self.assertRaises(ValueError):function_span('function init(){} function init(){}','init')
        with self.assertRaises(ValueError):apply_patch('function init(){}','init','function init(){}; evil()')
