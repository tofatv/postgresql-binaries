#!/usr/bin/env python3
"""Make a macOS PostgreSQL install relocatable and self-contained.

After `make install` the tree references libraries in two ways that break on
any machine other than the build runner:

1. Homebrew dependencies by absolute path (e.g.
   /opt/homebrew/opt/openssl@3/lib/libssl.3.dylib). A user without Homebrew
   cannot load them at all, and consumers that code-sign the tree with the
   hardened runtime reject Homebrew's dylibs outright (different Team IDs).
2. The build prefix itself (/Users/runner/work/.../lib/libpq.5.dylib) as the
   install name of, and in references between, the built dylibs. The old
   fix-up only rewrote bin/* references to libpq, so lib/*.dylib kept the
   runner path (visible in shipped 18.3.0: libpq.5.dylib's own id, and
   libecpg/libpgtypes references).

This script walks every Mach-O in the install, copies each non-system
dependency into lib/, and rewrites all references relative to the file that
holds them (@executable_path/../lib for bin/, @loader_path for lib/),
iterating over the copied libraries' own dependencies to a fixpoint. It then
verifies nothing absolute remains and re-signs every modified file ad hoc
(mandatory on arm64: install_name_tool invalidates the signature).

Usage: relocate_macos.py <install_directory>
"""

import os
import re
import shutil
import subprocess
import sys

SYSTEM_PREFIXES = ("/usr/lib/", "/System/")
DEP_RE = re.compile(r"^\t(\S+) \(compatibility")


def run(*argv):
    subprocess.run(argv, check=True, capture_output=True, text=True)


def is_macho(path):
    try:
        with open(path, "rb") as fh:
            magic = fh.read(4)
    except OSError:
        return False
    return magic in (
        b"\xcf\xfa\xed\xfe",  # MH_MAGIC_64 (little endian on disk)
        b"\xfe\xed\xfa\xcf",
        b"\xca\xfe\xba\xbe",  # fat
        b"\xbe\xba\xfe\xca",
    )


def deps_of(path):
    out = subprocess.run(
        ["otool", "-L", path], check=True, capture_output=True, text=True
    ).stdout
    deps = [m.group(1) for m in map(DEP_RE.match, out.splitlines()) if m]
    # For a dylib the first entry is its own install name; drop it so we only
    # rewrite genuine references (the id is fixed separately).
    own_id = own_id_of(path)
    return [d for d in deps if d != own_id]


def own_id_of(path):
    out = subprocess.run(
        ["otool", "-D", path], check=True, capture_output=True, text=True
    ).stdout.splitlines()
    return out[1].strip() if len(out) > 1 else None


def main(install_dir):
    install_dir = os.path.realpath(install_dir)
    lib_dir = os.path.join(install_dir, "lib")
    bin_dir = os.path.join(install_dir, "bin")

    machos = []
    for root, _dirs, files in os.walk(install_dir):
        for name in files:
            path = os.path.join(root, name)
            if not os.path.islink(path) and is_macho(path):
                machos.append(path)

    modified = set()
    queue = list(machos)
    while queue:
        path = queue.pop()
        for dep in deps_of(path):
            if dep.startswith(("@executable_path", "@loader_path", "@rpath")):
                continue
            if dep.startswith(SYSTEM_PREFIXES):
                continue
            base = os.path.basename(dep)
            local = os.path.join(lib_dir, base)
            if not os.path.exists(local):
                # A dependency outside the install (Homebrew): bring it in
                # and process its own dependencies too.
                if not os.path.exists(dep):
                    sys.exit(f"unresolvable dependency {dep} of {path}")
                shutil.copy2(os.path.realpath(dep), local)
                os.chmod(local, 0o755)
                queue.append(local)
                machos.append(local)
            if path.startswith(bin_dir + os.sep):
                new_ref = f"@executable_path/../lib/{base}"
            else:
                rel = os.path.relpath(lib_dir, os.path.dirname(path))
                rel = "" if rel == "." else rel + "/"
                new_ref = f"@loader_path/{rel}{base}"
            run("install_name_tool", "-change", dep, new_ref, path)
            modified.add(path)

    # Scrub build-prefix install names from the dylibs themselves.
    for path in machos:
        own_id = own_id_of(path)
        if own_id and not own_id.startswith(("@", *SYSTEM_PREFIXES)):
            run("install_name_tool", "-id", os.path.basename(own_id), path)
            modified.add(path)

    # install_name_tool invalidates code signatures, and arm64 refuses to run
    # unsigned binaries: re-sign everything we touched, ad hoc. Consumers that
    # ship these inside signed bundles re-sign with their own identity anyway.
    for path in sorted(modified):
        run("codesign", "--force", "--sign", "-", path)

    # Verify: nothing may reference Homebrew, /usr/local, or any absolute
    # non-system path (which includes the build prefix).
    bad = []
    for path in machos:
        for dep in deps_of(path):
            if dep.startswith(("@", *SYSTEM_PREFIXES)):
                continue
            bad.append(f"{path}: {dep}")
    if bad:
        sys.exit("non-relocatable references remain:\n" + "\n".join(bad))

    print(
        f"relocated {len(machos)} Mach-O files, rewrote {len(modified)}, "
        "verification clean"
    )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
