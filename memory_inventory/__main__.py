import argparse
from importlib import import_module

from . import import_all_stdlib_modules, main


p = argparse.ArgumentParser()
p.add_argument('--debug', action='store_true')
p.add_argument('--objects-limit', type=int, default=10_000_000)
p.add_argument('--save-dir')
p.add_argument('module_names', nargs='*', default=['*stdlib'], metavar='module_name')
args = p.parse_args()
for module_name in args.module_names:
    if module_name == '*stdlib':
        import_all_stdlib_modules()
    elif module_name:
        import_module(module_name)
if not args.save_dir:
    if args.module_names == ['*stdlib']:
        args.save_dir = '{tmpdir}/memory_inventory_python_{python_version}_stdlib/'
    else:
        args.save_dir = (
            './memory_inventory_%s_python_{python_version}/' %
            '-'.join(sorted(args.module_names))
        )
main(
    args.save_dir,
    write_debug_log=args.debug,
    objects_limit=args.objects_limit,
)
