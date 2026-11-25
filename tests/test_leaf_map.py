"""葉 -> Wikidata の対応表(data/wikidata-leaf-map.csv)の独立した整合検査。ネットワークには出ない。"""
import csv
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
MAP = ROOT / 'data' / 'wikidata-leaf-map.csv'
CATS = ROOT / 'data' / 'categories.csv'
COLUMNS = ['leaf_id', 'qid', 'rel', 'country_prop', 'subclass', 'label_en', 'note', 'max_rows']
RELS = {'P31', 'P106'}                     # ローダ(discover_leaf_directed.build_ids_query)が扱う関係
COUNTRY = {'P17|P495', 'P27'}              # 施設・団体は所在国/原産国、個人は国籍


def rows():
    with open(MAP, encoding='utf-8', newline='') as f:
        return list(csv.DictReader(f))


def leaves():
    with open(CATS, encoding='utf-8', newline='') as f:
        cats = list(csv.DictReader(f))
    parents = {c['parent_id'] for c in cats}
    return {c['id'] for c in cats if c['assign'] == 'llm' and c['id'] not in parents}


class LeafMap(unittest.TestCase):
    def test_header(self):
        with open(MAP, encoding='utf-8', newline='') as f:
            self.assertEqual(next(csv.reader(f)), COLUMNS)

    def test_rows_are_valid(self):
        ok_leaf = leaves()
        for i, r in enumerate(rows(), 2):
            w = f'行{i} {r["leaf_id"]}/{r["qid"]}'
            self.assertIn(r['leaf_id'], ok_leaf, w)
            self.assertRegex(r['qid'], r'^Q[1-9]\d*$', w)
            self.assertIn(r['rel'], RELS, w)
            self.assertIn(r['country_prop'], COUNTRY, w)
            self.assertIn(r['subclass'], ('0', '1'), w)
            if r['rel'] == 'P106':
                self.assertEqual(r['subclass'], '0', w)
                self.assertEqual(r['country_prop'], 'P27', w)   # 人物は国籍で絞る
            else:
                self.assertEqual(r['country_prop'], 'P17|P495', w)
            self.assertRegex(r['max_rows'], r'^[1-9]\d*$', w)
            self.assertTrue(r['label_en'].strip() and r['note'].strip(), w)

    def test_no_duplicates(self):
        keys = [(r['leaf_id'], r['qid'], r['rel']) for r in rows()]
        self.assertEqual(len(keys), len(set(keys)))


if __name__ == '__main__':
    unittest.main()
