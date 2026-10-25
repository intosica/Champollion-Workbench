#!/usr/bin/env python3
"""
extract_ba2.py -- Extract Bethesda 'BTDX' GNRL BA2 archives.

Usage:
    python3 extract_ba2.py <output_directory> <archive1.ba2> [archive2.ba2 ...]

Each archive is extracted into <output_directory>, preserving the relative
paths stored in the archive's name table. Multiple archives can be given at
once; they're extracted one after another into the same output tree (this
is normal -- e.g. a "Main.ba2" and "Textures.ba2" for the same mod land
side by side without conflicting).

Only the 'GNRL' (general) BA2 type is supported, matching the original
single-archive script this was built from. 'DX10' (texture) archives and
LZ4-compressed entries (used by some Fallout 4 archives) are not handled.
"""
import sys
import os
import struct
import zlib


def extract_one(ba2_path, output_dir):
    """Extract a single archive. Returns the number of files extracted, or
    None on failure (after printing an error)."""
    with open(ba2_path, 'rb') as f:
        header_data = f.read(24)
        if len(header_data) < 24:
            print(f"Error: {ba2_path} is too small to be a valid BA2.")
            return None

        magic, version, ba2_type, num_files, name_table_offset = struct.unpack(
            '<4sI4sIQ', header_data
        )

        if magic != b'BTDX':
            print(f"Error: {ba2_path} is not a valid BA2 archive.")
            return None

        print(
            f"{os.path.basename(ba2_path)} -- "
            f"Type: {ba2_type.decode('ascii', errors='ignore')} | Files: {num_files}"
        )

        if ba2_type != b'GNRL':
            print(
                f"Error: {ba2_path} is a '{ba2_type.decode('ascii', errors='ignore')}' "
                "archive; only 'GNRL' archives are supported."
            )
            return None

        files = []
        for _ in range(num_files):
            entry_data = f.read(36)
            name_hash, ext, dir_hash, flags, offset, packed_size, unpacked_size, align = struct.unpack(
                '<I4sIIQIII', entry_data
            )
            files.append({
                'offset': offset,
                'packed_size': packed_size,
                'unpacked_size': unpacked_size,
            })

        f.seek(name_table_offset)
        for i in range(num_files):
            name_len = struct.unpack('<H', f.read(2))[0]
            files[i]['path'] = f.read(name_len).decode('utf-8', errors='ignore')

        extracted_count = 0
        for entry in files:
            file_path = os.path.normpath(entry['path'])
            dest_path = os.path.join(output_dir, file_path)
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)

            f.seek(entry['offset'])
            if entry['packed_size'] > 0:
                raw_data = f.read(entry['packed_size'])
                try:
                    data = zlib.decompress(raw_data)
                except zlib.error as e:
                    print(f"  ! Skipping {entry['path']}: decompression failed ({e})")
                    continue
            else:
                data = f.read(entry['unpacked_size'])

            with open(dest_path, 'wb') as out_f:
                out_f.write(data)
            extracted_count += 1

        return extracted_count


def main(argv):
    if len(argv) < 3:
        print(
            "Usage: python3 extract_ba2.py <output_directory> "
            "<archive1.ba2> [archive2.ba2 ...]"
        )
        return 1

    output_dir = argv[1]
    archives = argv[2:]
    os.makedirs(output_dir, exist_ok=True)

    total = 0
    failed = []
    for archive in archives:
        print(f"\n=== {archive} ===")
        sys.stdout.flush()
        if not os.path.isfile(archive):
            print(f"Error: No such file: {archive}")
            failed.append(archive)
            continue
        try:
            count = extract_one(archive, output_dir)
        except Exception as e:  # noqa: BLE001
            print(f"Error extracting {archive}: {e}")
            failed.append(archive)
            continue
        if count is None:
            failed.append(archive)
        else:
            total += count
            print(f"Extracted {count} files from {os.path.basename(archive)}.")
        sys.stdout.flush()

    print(f"\nDone. {total} files extracted from {len(archives) - len(failed)}/{len(archives)} archive(s) to '{output_dir}'.")
    if failed:
        print("Failed archives:")
        for a in failed:
            print(f"  - {a}")
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
