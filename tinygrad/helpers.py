from __future__ import annotations
import os, functools, platform, time, re, contextlib, operator, hashlib, pickle, sqlite3, tempfile, pathlib, string, ctypes, sys, gzip
import itertools, urllib.request, subprocess, shutil, math, json, contextvars
from dataclasses import dataclass
from typing import Dict, Tuple, Union, List, ClassVar, Optional, Iterable, Any, TypeVar, TYPE_CHECKING, Callable, Sequence
if TYPE_CHECKING:  # TODO: remove this and import TypeGuard from typing once minimum python supported version is 3.10
  from typing_extensions import TypeGuard
  from tinygrad.shape.shapetracker import sint

T = TypeVar("T")
U = TypeVar("U")
# NOTE: it returns int 1 if x is empty regardless of the type of x
def prod(x:Iterable[T]) -> Union[T,int]: return functools.reduce(operator.mul, x, 1)

# NOTE: helpers is not allowed to import from anything else in tinygrad
OSX = platform.system() == "Darwin"
CI = os.getenv("CI", "") != ""

def dedup(x:Iterable[T]): return list(dict.fromkeys(x))   # retains list order
def argfix(*x):
  if x and x[0].__class__ in (tuple, list):
    if len(x) != 1: raise ValueError(f"bad arg {x}")
    return tuple(x[0])
  return x
def argsort(x): return type(x)(sorted(range(len(x)), key=x.__getitem__)) # https://stackoverflow.com/questions/3382352/equivalent-of-numpy-argsort-in-basic-python
def all_same(items:Union[Tuple[T, ...], List[T]]): return all(x == items[0] for x in items)
def all_int(t: Sequence[Any]) -> TypeGuard[Tuple[int, ...]]: return all(isinstance(s, int) for s in t)
def colored(st, color:Optional[str], background=False): return f"\u001b[{10*background+60*(color.upper() == color)+30+['black', 'red', 'green', 'yellow', 'blue', 'magenta', 'cyan', 'white'].index(color.lower())}m{st}\u001b[0m" if color is not None else st  # replace the termcolor library with one line  # noqa: E501
def colorize_float(x: float): return colored(f"{x:7.2f}x", 'green' if x < 0.75 else 'red' if x > 1.15 else 'yellow')
def memsize_to_str(_bytes: int) -> str: return [f"{(_bytes / d):.2f} {pr}" for d,pr in [(1e9,"GB"),(1e6,"MB"),(1e3,"KB"),(1,"B")] if _bytes > d][0]
def ansistrip(s:str): return re.sub('\x1b\\[(K|.*?m)', '', s)
def ansilen(s:str): return len(ansistrip(s))
def make_pair(x:Union[int, Tuple[int, ...]], cnt=2) -> Tuple[int, ...]: return (x,)*cnt if isinstance(x, int) else x
def flatten(l:Iterable[Iterable[T]]): return [item for sublist in l for item in sublist]
def fully_flatten(l): return [item for sublist in l for item in (fully_flatten(sublist) if isinstance(sublist, (tuple, list)) else [sublist])]
def fromimport(mod, frm): return getattr(__import__(mod, fromlist=[frm]), frm)
def strip_parens(fst:str): return fst[1:-1] if fst[0] == '(' and fst[-1] == ')' and fst[1:-1].find('(') <= fst[1:-1].find(')') else fst
def round_up(num, amt:int): return (num+amt-1)//amt * amt
def data64(data: int) -> Tuple[int, int]: return (data >> 32, data & 0xFFFFFFFF)
def data64_le(data: int) -> Tuple[int, int]: return (data & 0xFFFFFFFF, data >> 32)
def merge_dicts(ds:Iterable[Dict[T,U]]) -> Dict[T,U]:
  kvs = set([(k,v) for d in ds for k,v in d.items()])
  assert len(kvs) == len(set(kv[0] for kv in kvs)), f"cannot merge, {kvs} contains different values for the same key"
  return {k:v for d in ds for k,v in d.items()}
def partition(itr:Iterable[T], fxn:Callable[[T],bool]) -> Tuple[List[T], List[T]]:
  a:List[T] = []
  b:List[T] = []
  for s in itr: (a if fxn(s) else b).append(s)
  return a,b
def unwrap(x:Optional[T]) -> T:
  assert x is not None
  return x
def unwrap2(x:Tuple[T,Any]) -> T:
  ret, err = x
  assert err is None, str(err)
  return ret
def get_child(obj, key):
  for k in key.split('.'):
    if k.isnumeric(): obj = obj[int(k)]
    elif isinstance(obj, dict): obj = obj[k]
    else: obj = getattr(obj, k)
  return obj

def get_shape(x) -> Tuple[int, ...]:
  if not isinstance(x, (list, tuple)): return ()
  subs = [get_shape(xi) for xi in x]
  if not all_same(subs): raise ValueError(f"inhomogeneous shape from {x}")
  return (len(subs),) + (subs[0] if subs else ())

# returns the axes to create new_shape if new_shape can be created by combining axis from old_shape
def get_contraction(old_shape:Tuple[sint, ...], new_shape:Tuple[sint, ...]) -> Optional[List[List[int]]]:
  acc_old, acc_new = list(itertools.accumulate(old_shape, operator.mul)), list(itertools.accumulate(new_shape, operator.mul))
  try: split = [acc_old.index(acc)+1 if acc != 1 else 0 for acc in acc_new]
  except ValueError: return None
  return [list(range(st,ed)) for st,ed in zip([0]+split[:-1], split[:-1]+[len(old_shape)])]

@functools.lru_cache(maxsize=None)
def to_function_name(s:str): return ''.join([c if c in (string.ascii_letters+string.digits+'_') else f'{ord(c):02X}' for c in ansistrip(s)])
@functools.lru_cache(maxsize=None)
def getenv(key:str, default=0): return type(default)(os.getenv(key, default))
def temp(x:str) -> str: return (pathlib.Path(tempfile.gettempdir()) / x).as_posix()

class Context(contextlib.ContextDecorator):
  stack: ClassVar[List[dict[str, int]]] = [{}]
  def __init__(self, **kwargs): self.kwargs = kwargs
  def __enter__(self):
    Context.stack[-1] = {k:o.value for k,o in ContextVar._cache.items()} # Store current state.
    for k,v in self.kwargs.items(): ContextVar._cache[k].value = v # Update to new temporary state.
    Context.stack.append(self.kwargs) # Store the temporary state so we know what to undo later.
  def __exit__(self, *args):
    for k in Context.stack.pop(): ContextVar._cache[k].value = Context.stack[-1].get(k, ContextVar._cache[k].value)

class ContextVar:
  _cache: ClassVar[Dict[str, ContextVar]] = {}
  value: int
  key: str
  def __new__(cls, key, default_value):
    if key in ContextVar._cache: return ContextVar._cache[key]
    instance = ContextVar._cache[key] = super().__new__(cls)
    instance.value, instance.key = getenv(key, default_value), key
    return instance
  def __bool__(self): return bool(self.value)
  def __ge__(self, x): return self.value >= x
  def __gt__(self, x): return self.value > x
  def __lt__(self, x): return self.value < x

DEBUG, IMAGE, BEAM, NOOPT, JIT = ContextVar("DEBUG", 0), ContextVar("IMAGE", 0), ContextVar("BEAM", 0), ContextVar("NOOPT", 0), ContextVar("JIT", 1)
WINO, THREEFRY, CAPTURING, TRACEMETA = ContextVar("WINO", 0), ContextVar("THREEFRY", 0), ContextVar("CAPTURING", 1), ContextVar("TRACEMETA", 1)
GRAPH, GRAPHPATH, SAVE_SCHEDULE, RING = ContextVar("GRAPH", 0), getenv("GRAPHPATH", "/tmp/net"), ContextVar("SAVE_SCHEDULE", 0), ContextVar("RING", 1)
MULTIOUTPUT, PROFILE, PROFILEPATH = ContextVar("MULTIOUTPUT", 1), ContextVar("PROFILE", 0), ContextVar("PROFILEPATH", temp("tinygrad_profile.json"))
USE_TC, TC_OPT, TRANSCENDENTAL = ContextVar("TC", 1), ContextVar("TC_OPT", 0), ContextVar("TRANSCENDENTAL", 1)
FUSE_ARANGE, FUSE_CONV_BW = ContextVar("FUSE_ARANGE", 0), ContextVar("FUSE_CONV_BW", 0)
SPLIT_REDUCEOP, ARANGE_DIFF = ContextVar("SPLIT_REDUCEOP", 1), ContextVar("ARANGE_DIFF", 0)

@dataclass(frozen=True)
class Metadata:
  name: str
  caller: str
  backward: bool = False
  def __hash__(self): return hash(self.name)
  def __repr__(self): return str(self) + (f" - {self.caller}" if self.caller else "")
  def __str__(self): return self.name + (" bw" if self.backward else "")
_METADATA: contextvars.ContextVar[Optional[Metadata]] = contextvars.ContextVar("_METADATA", default=None)

# **************** global state Counters ****************

class GlobalCounters:
  global_ops: ClassVar[int] = 0
  global_mem: ClassVar[int] = 0
  time_sum_s: ClassVar[float] = 0.0
  kernel_count: ClassVar[int] = 0
  mem_used: ClassVar[int] = 0   # NOTE: this is not reset
  @staticmethod
  def reset(): GlobalCounters.global_ops, GlobalCounters.global_mem, GlobalCounters.time_sum_s, GlobalCounters.kernel_count = 0,0,0.0,0

# **************** timer and profiler ****************

class Timing(contextlib.ContextDecorator):
  def __init__(self, prefix="", on_exit=None, enabled=True): self.prefix, self.on_exit, self.enabled = prefix, on_exit, enabled
  def __enter__(self): self.st = time.perf_counter_ns()
  def __exit__(self, *exc):
    self.et = time.perf_counter_ns() - self.st
    if self.enabled: print(f"{self.prefix}{self.et*1e-6:6.2f} ms"+(self.on_exit(self.et) if self.on_exit else ""))

def _format_fcn(fcn): return f"{fcn[0]}:{fcn[1]}:{fcn[2]}"
class Profiling(contextlib.ContextDecorator):
  def __init__(self, enabled=True, sort='cumtime', frac=0.2, fn=None, ts=1):
    self.enabled, self.sort, self.frac, self.fn, self.time_scale = enabled, sort, frac, fn, 1e3/ts
  def __enter__(self):
    import cProfile
    self.pr = cProfile.Profile()
    if self.enabled: self.pr.enable()
  def __exit__(self, *exc):
    if self.enabled:
      self.pr.disable()
      if self.fn: self.pr.dump_stats(self.fn)
      import pstats
      stats = pstats.Stats(self.pr).strip_dirs().sort_stats(self.sort)
      for fcn in stats.fcn_list[0:int(len(stats.fcn_list)*self.frac)]:    # type: ignore[attr-defined]
        (_primitive_calls, num_calls, tottime, cumtime, callers) = stats.stats[fcn]    # type: ignore[attr-defined]
        scallers = sorted(callers.items(), key=lambda x: -x[1][2])
        print(f"n:{num_calls:8d}  tm:{tottime*self.time_scale:7.2f}ms  tot:{cumtime*self.time_scale:7.2f}ms",
              colored(_format_fcn(fcn).ljust(50), "yellow"),
              colored(f"<- {(scallers[0][1][2]/tottime)*100:3.0f}% {_format_fcn(scallers[0][0])}", "BLACK") if scallers else '')

class ProfileLogger:
  writers: int = 0
  mjson: List[Dict] = []
  actors: Dict[Union[str, Tuple[str, str]], int] = {}

  def __init__(self): self.events, self.deps, ProfileLogger.writers = [], [], ProfileLogger.writers + 1

  def add_event(self, ev_name, ev_start, ev_end, actor, subactor=None, args=None): self.events += [(ev_name, ev_start, ev_end, actor, subactor, args)]

  def _ensure_actor(self, actor_name, subactor_name):
    if actor_name not in self.actors:
      self.actors[actor_name] = (pid:=len(self.actors))
      self.mjson.append({"name": "process_name", "ph": "M", "pid": pid, "args": {"name": actor_name}})

    if (subactor_key:=(actor_name,subactor_name)) not in self.actors:
      self.actors[subactor_key] = (tid:=len(self.actors))
      self.mjson.append({"name": "thread_name", "ph": "M", "pid": self.actors[actor_name], "tid":tid, "args": {"name": subactor_name}})

    return self.actors[actor_name], self.actors.get(subactor_key, -1)

  def __del__(self):
    # perfetto json docs: https://docs.google.com/document/d/1CvAClvFfyA5R-PhYUmn5OOQtYMH4h6I0nSsKchNAySU/preview
    for name, st, et, actor_name, subactor_name, args in self.events:
      pid, tid = self._ensure_actor(actor_name,subactor_name)
      args = {k: (v if v.__class__ is str else v(et-st)) for k, v in args.items()} if args is not None else None
      self.mjson.append({"name": name, "ph": "X", "pid": pid, "tid": tid, "ts": st, "dur": et-st, "args": args})

    for en,st,dep_actor_name,dep_subactor_name,actor_name,subactor_name in self.deps:
      dep_pid, dep_tid = self._ensure_actor(dep_actor_name,dep_subactor_name)
      pid, tid = self._ensure_actor(actor_name,subactor_name)
      self.mjson.append({"ph": "s", "pid": dep_pid, "tid": dep_tid, "id": len(self.mjson), "ts": en, "bp": "e"})
      self.mjson.append({"ph": "f", "pid": pid, "tid": tid, "id": len(self.mjson)-1, "ts": st, "bp": "e"})

    ProfileLogger.writers -= 1
    if ProfileLogger.writers == 0 and len(self.mjson) > 0:
      with open(PROFILEPATH.value, "w") as f: f.write(json.dumps({"traceEvents": self.mjson}))
      print(f"Saved profile to {PROFILEPATH.value}. Use https://ui.perfetto.dev/ to open it.")

# *** universal database cache ***

_cache_dir: str = getenv("XDG_CACHE_HOME", os.path.expanduser("~/Library/Caches" if OSX else "~/.cache"))
CACHEDB: str = getenv("CACHEDB", os.path.abspath(os.path.join(_cache_dir, "tinygrad", "cache.db")))
CACHELEVEL = getenv("CACHELEVEL", 2)

VERSION = 16
_db_connection = None
def db_connection():
  global _db_connection
  if _db_connection is None:
    os.makedirs(CACHEDB.rsplit(os.sep, 1)[0], exist_ok=True)
    _db_connection = sqlite3.connect(CACHEDB, timeout=60, isolation_level="IMMEDIATE")
    # another connection has set it already or is in the process of setting it
    # that connection will lock the database
    with contextlib.suppress(sqlite3.OperationalError): _db_connection.execute("PRAGMA journal_mode=WAL").fetchone()
    if DEBUG >= 7: _db_connection.set_trace_callback(print)
  return _db_connection

def diskcache_clear():
  cur = db_connection().cursor()
  drop_tables = cur.execute("SELECT 'DROP TABLE IF EXISTS ' || quote(name) || ';' FROM sqlite_master WHERE type = 'table';").fetchall()
  cur.executescript("\n".join([s[0] for s in drop_tables]))

def diskcache_get(table:str, key:Union[Dict, str, int]) -> Any:
  if CACHELEVEL == 0: return None
  if isinstance(key, (str,int)): key = {"key": key}
  conn = db_connection()
  cur = conn.cursor()
  try:
    res = cur.execute(f"SELECT val FROM '{table}_{VERSION}' WHERE {' AND '.join([f'{x}=?' for x in key.keys()])}", tuple(key.values()))
  except sqlite3.OperationalError:
    return None  # table doesn't exist
  if (val:=res.fetchone()) is not None: return pickle.loads(val[0])
  return None

_db_tables = set()
def diskcache_put(table:str, key:Union[Dict, str, int], val:Any):
  if CACHELEVEL == 0: return val
  if isinstance(key, (str,int)): key = {"key": key}
  conn = db_connection()
  cur = conn.cursor()
  if table not in _db_tables:
    TYPES = {str: "text", bool: "integer", int: "integer", float: "numeric", bytes: "blob"}
    ltypes = ', '.join(f"{k} {TYPES[type(key[k])]}" for k in key.keys())
    cur.execute(f"CREATE TABLE IF NOT EXISTS '{table}_{VERSION}' ({ltypes}, val blob, PRIMARY KEY ({', '.join(key.keys())}))")
    _db_tables.add(table)
  cur.execute(f"REPLACE INTO '{table}_{VERSION}' ({', '.join(key.keys())}, val) VALUES ({', '.join(['?']*len(key.keys()))}, ?)", tuple(key.values()) + (pickle.dumps(val), ))  # noqa: E501
  conn.commit()
  cur.close()
  return val

def diskcache(func):
  def wrapper(*args, **kwargs) -> bytes:
    table, key = f"cache_{func.__name__}", hashlib.sha256(pickle.dumps((args, kwargs))).hexdigest()
    if (ret:=diskcache_get(table, key)): return ret
    return diskcache_put(table, key, func(*args, **kwargs))
  return wrapper

# *** http support ***

def fetch(url:str, name:Optional[Union[pathlib.Path, str]]=None, subdir:Optional[str]=None, gunzip:bool=False,
          allow_caching=not getenv("DISABLE_HTTP_CACHE")) -> pathlib.Path:
  if url.startswith(("/", ".")): return pathlib.Path(url)
  if name is not None and (isinstance(name, pathlib.Path) or '/' in name): fp = pathlib.Path(name)
  else:
    fp = pathlib.Path(_cache_dir) / "tinygrad" / "downloads" / (subdir or "") / \
      ((name or hashlib.md5(url.encode('utf-8')).hexdigest()) + (".gunzip" if gunzip else ""))
  if not fp.is_file() or not allow_caching:
    with urllib.request.urlopen(url, timeout=10) as r:
      assert r.status == 200
      total_length = int(r.headers.get('content-length', 0))
      progress_bar = tqdm(total=total_length, unit='B', unit_scale=True, desc=f"{url}", disable=CI)
      (path := fp.parent).mkdir(parents=True, exist_ok=True)
      readfile = gzip.GzipFile(fileobj=r) if gunzip else r
      with tempfile.NamedTemporaryFile(dir=path, delete=False) as f:
        while chunk := readfile.read(16384): progress_bar.update(f.write(chunk))
        f.close()
        progress_bar.update(close=True)
        if (file_size:=os.stat(f.name).st_size) < total_length: raise RuntimeError(f"fetch size incomplete, {file_size} < {total_length}")
        pathlib.Path(f.name).rename(fp)
  return fp

# *** Exec helpers

def cpu_time_execution(cb, enable):
  if enable: st = time.perf_counter()
  cb()
  if enable: return time.perf_counter()-st

def cpu_objdump(lib):
  with tempfile.NamedTemporaryFile(delete=True) as f:
    pathlib.Path(f.name).write_bytes(lib)
    print(subprocess.check_output(['objdump', '-d', f.name]).decode('utf-8'))

# *** ctypes helpers

# TODO: make this work with read only memoryviews (if possible)
def from_mv(mv:memoryview, to_type=ctypes.c_char):
  return ctypes.cast(ctypes.addressof(to_type.from_buffer(mv)), ctypes.POINTER(to_type * len(mv))).contents
def to_mv(ptr, sz) -> memoryview: return memoryview(ctypes.cast(ptr, ctypes.POINTER(ctypes.c_uint8 * sz)).contents).cast("B")
def mv_address(mv:memoryview): return ctypes.addressof(ctypes.c_char.from_buffer(mv))
def to_char_p_p(options: List[bytes], to_type=ctypes.c_char): return (ctypes.POINTER(to_type) * len(options))(*[ctypes.cast(ctypes.create_string_buffer(o), ctypes.POINTER(to_type)) for o in options])  # noqa: E501
@functools.lru_cache(maxsize=None)
def init_c_struct_t(fields: Tuple[Tuple[str, ctypes._SimpleCData], ...]):
  class CStruct(ctypes.Structure):
    _pack_, _fields_ = 1, fields
  return CStruct
def init_c_var(ctypes_var, creat_cb): return (creat_cb(ctypes_var), ctypes_var)[1]
def flat_mv(mv:memoryview): return mv if len(mv) == 0 else mv.cast("B", shape=(mv.nbytes,))

# *** tqdm

class tqdm:
  def __init__(self, iterable=None, desc:str='', disable:bool=False, unit:str='it', unit_scale=False, total:Optional[int]=None, rate:int=100):
    self.iterable, self.disable, self.unit, self.unit_scale, self.rate = iterable, disable, unit, unit_scale, rate
    self.st, self.i, self.n, self.skip, self.t = time.perf_counter(), -1, 0, 1, getattr(iterable, "__len__", lambda:0)() if total is None else total
    self.set_description(desc)
    self.update(0)
  def __iter__(self):
    for item in self.iterable:
      yield item
      self.update(1)
    self.update(close=True)
  def set_description(self, desc:str): self.desc = f"{desc}: " if desc else ""
  def update(self, n:int=0, close:bool=False):
    self.n, self.i = self.n+n, self.i+1
    if self.disable or (not close and self.i % self.skip != 0): return
    prog, elapsed, ncols = self.n/self.t if self.t else 0, time.perf_counter()-self.st, shutil.get_terminal_size().columns
    if self.i/elapsed > self.rate and self.i: self.skip = max(int(self.i/elapsed)//self.rate,1)
    def HMS(t): return ':'.join(f'{x:02d}' if i else str(x) for i,x in enumerate([int(t)//3600,int(t)%3600//60,int(t)%60]) if i or x)
    def SI(x): return (f"{x/1000**int(g:=math.log(x,1000)):.{int(3-3*math.fmod(g,1))}f}"[:4].rstrip('.')+' kMGTPEZY'[int(g)].strip()) if x else '0.00'
    prog_text = f'{SI(self.n)}{f"/{SI(self.t)}" if self.t else self.unit}' if self.unit_scale else f'{self.n}{f"/{self.t}" if self.t else self.unit}'
    elapsed_text = HMS(elapsed) + (f'<{HMS(elapsed/prog-elapsed) if self.n else "?"}' if self.t else '')
    it_text = (SI(self.n/elapsed) if self.unit_scale else f"{self.n/elapsed:5.2f}") if self.n else "?"
    suf = f'{prog_text} [{elapsed_text}, {it_text}{self.unit}/s]'
    sz = max(ncols-len(self.desc)-3-2-2-len(suf), 1)
    bar = '\r' + self.desc + (f'{100*prog:3.0f}%|{("█"*int(num:=sz*prog)+" ▏▎▍▌▋▊▉"[int(8*num)%8].strip()).ljust(sz," ")}| ' if self.t else '') + suf
    print(bar[:ncols+1], flush=True, end='\n'*close, file=sys.stderr)

class trange(tqdm):
  def __init__(self, n:int, **kwargs): super().__init__(iterable=range(n), total=n, **kwargs)

def pretty_print(x:Any, rep:Callable, srcfn=lambda x: x.src, cache=None, d=0)->str:
  def dfs(x:Any, cache:dict):
    for s in srcfn(x) or []:
      cache.setdefault(s, [len(cache), 0, False])[1] += 1
      if cache[s][1] == 1: dfs(s, cache)
  if cache is None: dfs(x, cache:={})
  if (cx:=cache.setdefault(x, [0,0,False]))[2]: return f"{' '*d} x{cx[0]}"
  cx[2], srcs = True, ('None' if srcfn(x) is None else ''.join(f'\n{pretty_print(s, rep, srcfn, cache, d+2)},' for s in srcfn(x)))
  return f"{' '*d}{f'x{cx[0]}:=' * (cx[1]>1)}{rep(x)}" % srcs

# *** objc
# note: The Objective-C runtime does not expose enough information to provide completely automatic bindings of all APIs. source: https://pyobjc.readthedocs.io/en/latest/metadata/index.html
from ctypes.util import find_library


# import tinygrad.runtime.autogen.objc as objc

libobjc = ctypes.CDLL(find_library("objc"))
libobjc.objc_msgSend.restype, libobjc.objc_msgSend.argtypes = ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_void_p]
libobjc.objc_getClass.restype, libobjc.objc_getClass.argtypes = ctypes.c_void_p, [ctypes.c_char_p]
libobjc.class_copyMethodList.restype, libobjc.class_copyMethodList.argtypes = ctypes.POINTER(ctypes.c_void_p), [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
libobjc.class_getName.restype, libobjc.class_getName.argtypes = ctypes.c_char_p, [ctypes.c_void_p]
libobjc.sel_registerName.restype, libobjc.sel_registerName.argtypes = ctypes.c_void_p, [ctypes.c_char_p]
libobjc.sel_getName.restype, libobjc.sel_getName.argtypes = ctypes.c_char_p, [ctypes.c_void_p]
libobjc.method_getName.restype, libobjc.method_getName.argtypes = ctypes.c_void_p, [ctypes.c_void_p]
libobjc.method_getTypeEncoding.restype, libobjc.method_getTypeEncoding.argtypes = ctypes.c_char_p, [ctypes.c_void_p]
libobjc.method_copyReturnType.restype, libobjc.method_copyReturnType.argtypes = ctypes.c_char_p, [ctypes.c_void_p]
libobjc.method_getNumberOfArguments.restype, libobjc.method_getNumberOfArguments.argtypes = ctypes.c_uint, [ctypes.c_void_p]
libobjc.method_copyArgumentType.restype, libobjc.method_copyArgumentType.argtypes = ctypes.c_char_p, [ctypes.c_void_p, ctypes.c_uint]
libobjc.object_getClassName.restype, libobjc.object_getClassName.argtypes = ctypes.c_char_p, [ctypes.c_void_p]
libobjc.object_getClass.restype, libobjc.object_getClass.argtypes = ctypes.c_void_p, [ctypes.c_void_p]
libobjc.class_getSuperclass.restype, libobjc.class_getSuperclass.argtypes = ctypes.c_void_p, [ctypes.c_void_p]

def isobjc(obj): return isinstance(obj, ObjcClass) or isinstance(obj, ObjcInstance)

def convert_arg(arg, type):
  if isinstance(arg, str) and type is ctypes.c_char_p: return arg.encode()
  if isinstance(arg, str) and type is ctypes.c_void_p: return NSString.stringWithUTF8String_(arg).ptr
  # if isinstance(arg, list) and type is ctypes.c_void_p: return (ctypes.c_void_p * len(arg))(*[a.ptr if isobjc(a) else a for a in arg])
  if isobjc(arg): return arg.ptr
  return arg

def objc_msgSend(obj, sel, *args, restype=None, argtypes=[]):
  base_argtypes = [ctypes.c_void_p, ctypes.c_void_p]
  encoded_args = [convert_arg(a, t) for a, t in zip(args, argtypes)]
  # print(f"Sending {sel}(restype:{restype} argtypes:{argtypes}) to ptr:{obj} with args:{args}")
  libobjc.objc_msgSend.restype, libobjc.objc_msgSend.argtypes = restype, ((base_argtypes + argtypes) if argtypes else base_argtypes)
  return libobjc.objc_msgSend(obj, libobjc.sel_registerName(sel.encode()), *encoded_args)

libc = ctypes.CDLL(find_library("c"))
libc.malloc.argtypes = [ctypes.c_size_t]
libc.malloc.restype = ctypes.c_void_p
libc.free.argtypes = [ctypes.c_void_p]

def dump_objc_methods(clz: ctypes.c_void_p):
  methods = {}
  method_count = ctypes.c_uint()
  methods_ptr = libobjc.class_copyMethodList(clz, ctypes.byref(method_count))
  assert methods_ptr is not None, f"Failed to get methods for class {clz}"
  class_name = libobjc.class_getName(clz).decode('ascii')
  # print(f"Found {method_count} methods on '{class_name}'")

  for i in range(method_count.value):
    method = methods_ptr[i]
    sel_name = libobjc.sel_getName(libobjc.method_getName(method)).decode('ascii')
    return_type_ptr = libobjc.method_copyReturnType(method)
    return_type = return_type_ptr.decode('ascii')
    argtypes_ptrs = [libobjc.method_copyArgumentType(method, i) for i in range(libobjc.method_getNumberOfArguments(method))]
    argtypes = [arg.decode('ascii') for arg in argtypes_ptrs]
    # print(f"\tMethod {i}: {sel_name} ({return_type} {argtypes})")
    methods[sel_name] = {"restype": return_type, "argtypes": argtypes}

    # _, _ = libc.free(ctypes.cast(return_type_ptr, c_void_p)), [libc.free(ctypes.cast(argtype, c_void_p)) for argtype in argtypes_ptrs]
  libc.free(methods_ptr)
  return methods


SIMPLE_TYPES = {
    'c': ctypes.c_char,
    'i': ctypes.c_int,
    's': ctypes.c_short,
    'l': ctypes.c_long,
    'q': ctypes.c_longlong,
    'C': ctypes.c_uint8,
    'I': ctypes.c_uint,
    'S': ctypes.c_ushort,
    'L': ctypes.c_ulong,
    'Q': ctypes.c_ulonglong,
    'f': ctypes.c_float,
    'd': ctypes.c_double,
    'B': ctypes.c_bool,
    'v': None,
    '*': ctypes.c_char_p,
    '@': ctypes.c_void_p,
    '#': 'Class',
    ':': 'SEL',
    '?': '<unknown-type>',
}

@functools.lru_cache(maxsize=None)
def get_methods_rec(c: ctypes.c_void_p):
  methods = {}
  while c:
    methods = {
      **methods,
      **dump_objc_methods(c)
    }
    c = libobjc.class_getSuperclass(c)
  return methods


def objc_type_to_ctype(t: str):
  if len(t) == 1:
    return SIMPLE_TYPES[t]
  elif t[0] == '^':
    return ctypes.POINTER(objc_type_to_ctype(t[1:]))
  elif t[0] == 'r':
    return objc_type_to_ctype(t[1:])
  elif t.startswith("{") and "=" in t and t.endswith("}"):
    return ctypes.Structure  # wooo! safety is out the window now
  else:
    raise ValueError(f"Unknown type {t}")


class ObjcClass:
  ptr: ctypes.c_void_p
  methods_info: Dict[str, Dict[str, Any]]

  def __init__(self, name:str):
    self.ptr = libobjc.objc_getClass(name.encode())
    assert self.ptr is not None, f"Class {name} not found"
    self.methods_info = get_methods_rec(_metaclass_ptr:=libobjc.object_getClass(self.ptr))

  @functools.lru_cache(maxsize=None)
  def __getattr__(self, name:str) -> Any:
    sel_name = name.replace("_", ":")
    if sel_name in self.methods_info:
      method_info = self.methods_info[sel_name]
      restype, argtypes = method_info["restype"], method_info["argtypes"]
      # print(f"Found method {name} with restype:{restype} argtypes:{argtypes}")
      f = functools.partial(objc_msgSend,
        self.ptr,
        sel_name,
        restype=objc_type_to_ctype(restype),
        argtypes=[objc_type_to_ctype(t) for t in argtypes[2:]])
      # ugly hack to conditionally wrap without self referencing recursion. e.g: "f = lambda *args: g(f(*args))"
      _f = (lambda *args, **kwargs: ObjcInstance(r) if (r:=f(*args, **kwargs)) is not None else None) if restype == "@" else f
      __f = (lambda *args, **kwargs: (_f(*args[:-1], ctypes.byref(err:=ctypes.c_void_p()), **kwargs), err if err.value else None)) if name.endswith("error_") else _f
      return __f

    raise AttributeError(f"Method {name} not found on {self.__class__.__name__}")


class ObjcInstance(ObjcClass):
  def __init__(self, ptr):
    assert ptr is not None, f"Can't create ObjcInstance with null ptr"
    self.ptr = ptr
    self.methods_info = get_methods_rec(libobjc.object_getClass(self.ptr))


NSString: Any = ObjcClass("NSString")

def nsstring_to_str(nsstring: ObjcClass) -> str:
    return ctypes.string_at(nsstring.UTF8String(), size=nsstring.length()).decode()
