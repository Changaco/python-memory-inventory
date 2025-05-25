`memory_inventory` is a Python module that collects and outputs statistics on
global objects. It can help you determine why a Python program consumes an
excessive amount of memory when it's idle. This is different than memory
profilers like [memray](https://bloomberg.github.io/memray/) that track memory
allocations as they happen.

`memory_inventory` is written entirely in typed Python and is compatible with
versions 3.12 and above of CPython.

## How it works

`memory_inventory` starts from [the `sys.modules` dictionary][sys.modules], adds
its keys and values to a queue, and accesses the native attributes of objects in
the queue in order to find more objects, until there are no more to find or a
pre-defined limit is reached. This should find all global objects in memory,
except those created by native code and not exposed to Python code.

Obviously this isn't the best way to take stock of Python objects in memory. It
isn't entirely accurate, it isn't fast, and it uses a significant amount of
memory, but the idea was to explore what could be done in pure Python without
using [the `ctypes` module][ctypes] to access raw memory words.

You should take a look at the source code of `memory_inventory`.

[sys.modules]: https://docs.python.org/3/library/sys.html#sys.modules
[ctypes]: https://docs.python.org/3/library/ctypes.html

## Installation

[`memory_inventory` is on PyPI](https://pypi.org/project/memory_inventory), so:

    python -m pip install memory_inventory

## Usage

### CLI

`memory_inventory` has a simple command line interface that can be used to load
a specific module and subsequently examine the global objects in memory:

    python -m memory_inventory $module_name

`memory_inventory` always excludes its own objects from the analysis, so asking
it to analyse itself doesn't work.

Running `memory_inventory` without specifying a module name launches an analysis
of the entire Python standard library:

    python -m memory_inventory

That last command should output something close to the following text when run
with CPython 3.13.3 on Linux:

```
Starting import of (almost) all the standard library modules
596 modules have been imported, increasing the total to 715.
14 modules failed to load: _overlapped, _pyrepl.windows_console, _scproxy, _winapi, _wmi, asyncio.windows_events, asyncio.windows_utils, encodings.mbcs, encodings.oem, msvcrt, multiprocessing.popen_spawn_win32, nt, winreg, winsound
Elapsed time: 0.222s (84.6% in userland). Resident memory: 60872 kB now, 61692 kB at peak. Active threads: 1.
The garbage collector is tracking 51452 objects and has previously deleted 478 others in 48 runs. It has found 0 uncollectable objects.
Types by intrinsic memory usage:
| type                       | cumulated memory usage | instances found | average memory usage per instance |
| ----------top 15---------- | ---------------------- | --------------- | --------------------------------- |
| str                        |            7_572_197 B |          82_021 |                              92 B |
| code                       |            6_784_072 B |          16_198 |                             418 B |
| bytes                      |            6_235_865 B |          34_906 |                             178 B |
| type                       |            4_009_648 B |           2_810 |                           1_426 B |
| tuple                      |            3_797_600 B |          51_369 |                              73 B |
| dict                       |            3_420_272 B |          21_467 |                             159 B |
| function                   |            2_485_600 B |          15_535 |                             160 B |
| set                        |              199_616 B |             232 |                             860 B |
| abc.ABCMeta                |              197_968 B |             143 |                           1_384 B |
| list                       |              176_312 B |           1_736 |                             101 B |
| int                        |              162_228 B |           5_702 |                              28 B |
| getset_descriptor          |              140_800 B |           2_200 |                              64 B |
| frozenset                  |              123_384 B |             269 |                             458 B |
| method_descriptor          |              105_840 B |           1_470 |                              72 B |
| builtin_function_or_method |               97_920 B |           1_360 |                              72 B |
| ..........total........... | ...................... | ............... | ................................. |
|                        311 |           36_401_131 B |         247_125 |                             147 B |
Uninstantiated types (excluding BaseException and its subclasses): 2178
Duplicate objects by type:
| type      | savable memory | duplicate instances |
| --------- | -------------- | ------------------- |
| tuple     |      845_032 B |              15_180 |
| bytes     |      585_127 B |               6_821 |
| str       |      449_390 B |               7_304 |
| int       |       75_880 B |               2_679 |
| frozenset |       32_552 B |                 127 |
| float     |        3_096 B |                 129 |
| complex   |            0 B |                   0 |
| ..total.. | .............. | ................... |
|         7 |    1_991_077 B |              32_240 |
Instantiated classes lacking slots:
| class                                       | savable memory | __dict__ memory usage | memory reduction | missing slots | instances found |
| -------------------top 15------------------ | -------------- | --------------------- | ---------------- | ------------- | --------------- |
| _frozen_importlib.ModuleSpec                |       51_336 B |             102_672 B |            50.0% |             9 |             713 |
| _frozen_importlib_external.SourceFileLoader |       43_128 B |              52_712 B |            81.8% |             2 |             599 |
| classmethod                                 |       38_088 B |              49_128 B |            77.5% |             5 |             276 |
| re._constants._NamedIntConstant             |       22_496 B |              23_104 B |            97.4% |             1 |              76 |
| http.HTTPStatus                             |       15_872 B |              18_848 B |            84.2% |             6 |              62 |
| staticmethod                                |       13_392 B |              21_712 B |            61.7% |             5 |             208 |
| tkinter.EventType                           |       10_064 B |              11_248 B |            89.5% |             4 |              37 |
| ssl._TLSAlertType                           |        9_248 B |              10_336 B |            89.5% |             4 |              34 |
| signal.Signals                              |        8_976 B |              10_032 B |            89.5% |             4 |              33 |
| socket.AddressFamily                        |        8_704 B |               9_728 B |            89.5% |             4 |              32 |
| functools._lru_cache_wrapper                |        7_488 B |               9_792 B |            76.5% |             8 |              36 |
| ssl.AlertDescription                        |        7_344 B |               8_208 B |            89.5% |             4 |              27 |
| ssl._TLSMessageType                         |        5_984 B |               6_688 B |            89.5% |             4 |              22 |
| ast._Precedence                             |        4_896 B |               5_472 B |            89.5% |             4 |              18 |
| inspect.BufferFlags                         |        4_624 B |               5_168 B |            89.5% |             4 |              17 |
| ...................total................... | .............. | ..................... | ................ | ............. | ............... |
|                                         148 |      372_928 B |             490_816 B |            76.0% |           829 |           2_848 |

Elapsed time: 8.455s (99.1% in userland). Resident memory: 182232 kB now, 184072 kB at peak. Active threads: 1.
The full results have been saved in /tmp/memory_inventory_python_3.13.3_stdlib/
```

### From Python

`memory_inventory` can be imported as a Python module, for example to do
something that the CLI doesn't support:

```python
import memory_inventory
memory_inventory.main(attributes_to_skip={id(SomeClass.some_attribute)})
```

## History

I started writing the `memory_inventory` module in 2022 as part of my work on
[Liberapay](https://github.com/liberapay/liberapay.com). I shelved it because I
had too many other things to do, and pretty much forgot about it. When I checked
it out and tried to run it again in April 2025, this time with Python 3.12, it
triggered [a segmentation fault](https://github.com/python/cpython/issues/132747).
This pushed me to finish and publish the module.

## Funding

`memory_inventory` wouldn't exist without the more than 1500 donors who support
the development of Liberapay. If you're one of them, I thank you. If you aren't,
please consider [chipping in](https://liberapay.com/Liberapay/donate).

## Copyright

Copyright [Charly Coste](https://github.com/Changaco), 2025, licensed under the
[EUPL](https://choosealicense.com/licenses/eupl-1.2/).
