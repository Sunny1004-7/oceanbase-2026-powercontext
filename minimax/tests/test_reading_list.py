"""Frozen business tests. READING_MODULE points at before/after snapshots."""
import copy, importlib.util, json, os, pathlib, subprocess, sys, unittest
ROOT=pathlib.Path(__file__).resolve().parents[1]
MODULE=pathlib.Path(os.environ.get('READING_MODULE',ROOT/'example/before/reading_list.py')).resolve()
spec=importlib.util.spec_from_file_location('reading_list',MODULE)
app=importlib.util.module_from_spec(spec); spec.loader.exec_module(app)
BOOKS=json.loads((ROOT/'example/books.json').read_text(encoding='utf-8'))
def ids(items):return [x['id'] for x in items]
def cli(*args):return subprocess.run([sys.executable,str(MODULE),str(ROOT/'example/books.json'),*args],capture_output=True,text=True,encoding='utf-8')

class RegressionTests(unittest.TestCase):
    def test_all_order(self):self.assertEqual(ids(app.select_books(BOOKS)),['b01','b02','b03','b04','b05','b06'])
    def test_status(self):self.assertEqual(ids(app.select_books(BOOKS,status='unread')),['b01','b03','b05'])
    def test_reading(self):self.assertEqual(ids(app.select_books(BOOKS,status='reading')),['b04'])
    def test_done(self):self.assertEqual(ids(app.select_books(BOOKS,status='done')),['b02','b06'])
    def test_invalid_status(self):
        with self.assertRaises(ValueError):app.select_books(BOOKS,status='bad')
    def test_empty(self):self.assertEqual(app.select_books([]),[])
    def test_immutable(self):
        books=copy.deepcopy(BOOKS); old=copy.deepcopy(books); app.select_books(books,status='done'); self.assertEqual(books,old)
    def test_cli_status(self):
        p=cli('--status','reading'); self.assertEqual(p.returncode,0,p.stderr); self.assertEqual(ids(json.loads(p.stdout)),['b04'])
    def test_cli_invalid(self):self.assertEqual(cli('--status','bad').returncode,2)

class TargetTests(unittest.TestCase):
    def test_tag(self):self.assertEqual(ids(app.select_books(BOOKS,tag='数据库')),['b01','b02','b04'])
    def test_intersection(self):self.assertEqual(ids(app.select_books(BOOKS,status='done',tag='数据库')),['b02'])
    def test_trim_v2(self):self.assertEqual(ids(app.select_books(BOOKS,tag='  数据库  ')),['b01','b02','b04'])
    def test_exact(self):self.assertEqual(ids(app.select_books(BOOKS,tag='数据库索引')),['b03'])
    def test_unknown(self):self.assertEqual(app.select_books(BOOKS,tag='不存在'),[])
    def test_case(self):self.assertEqual(app.select_books(BOOKS,tag='python'),[])
    def test_missing_tags(self):self.assertEqual(app.select_books([{'id':'x','status':'unread','added':'2026-01-01'}],tag='数据库'),[])
    def test_empty_tag(self):self.assertEqual(ids(app.select_books(BOOKS,tag='  ',status='done')),['b02','b06'])
    def test_duplicates(self):self.assertEqual(ids(app.select_books(BOOKS[:]+[{'id':'x','status':'done','added':'2026-11-01','tags':['独立','独立']}],tag='独立')),['x'])
    def test_input_unchanged(self):
        books=copy.deepcopy(BOOKS); old=copy.deepcopy(books); app.select_books(books,tag='数据库'); self.assertEqual(books,old)
    def test_cli_tag(self):
        p=cli('--tag','数据库'); self.assertEqual(p.returncode,0,p.stderr); self.assertEqual(ids(json.loads(p.stdout)),['b01','b02','b04'])
    def test_cli_both(self):
        p=cli('--tag','数据库','--status','unread'); self.assertEqual(p.returncode,0,p.stderr); self.assertEqual(ids(json.loads(p.stdout)),['b01'])

if __name__=='__main__':unittest.main()
