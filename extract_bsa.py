#!/usr/bin/env python3
"""
extract_bsa.py -- Extract Bethesda BSA archives (v103, v104, v105).
Sanitizes C-strings to prevent embedded null byte errors in os.makedirs().
"""

import sys
import os
import struct
import zlib

try:
    import lz4.block
    HAS_LZ4 = True
except ImportError:
    HAS_LZ4 = False

FLAG_HAS_NAMES   = 0x0001
FLAG_HAS_FILES   = 0x0002
FLAG_COMPRESSED  = 0x0004
FLAG_EMBED_NAMES = 0x0100


def clean_str(b_data):
    """Decode bytes and strip null terminators and non-printable characters."""
    return b_data.decode('ascii', errors='ignore').replace('\x00', '').strip()


def extract_one(bsa_path, output_dir):
    with open(bsa_path, 'rb') as f:
        header_data = f.read(36)
        if len(header_data) < 36:
            print(f"Error: {bsa_path} is too small to be a valid BSA.")
            return None

        (
            magic,
            version,
            header_offset,
            archive_flags,
            folder_count,
            file_count,
            total_folder_name_len,
            total_file_name_len,
            file_flags,
        ) = struct.unpack('<4sIIIIIIII', header_data)

        if magic != b'BSA\x00':
            print(f"Error: {bsa_path} is not a valid BSA archive.")
            return None

        if version not in (103, 104, 105):
            print(f"Error: BSA version {version} in {bsa_path} is not supported.")
            return None

        if version == 105 and not HAS_LZ4:
            print("Error: Version 105 (Skyrim SE) requires the 'lz4' Python package.")
            print("Run: pip install lz4")
            return None

        print(
            f"{os.path.basename(bsa_path)} -- "
            f"Version: {version} | Folders: {folder_count} | Files: {file_count}"
        )

        folder_records = []
        for _ in range(folder_count):
            rec = f.read(16)
            if len(rec) < 16:
                break
            f_hash, f_count, f_offset = struct.unpack('<QII', rec)
            folder_records.append({'count': f_count, 'offset': f_offset})

        file_entries = []
        has_dir_names = bool(archive_flags & FLAG_HAS_NAMES)

        for folder in folder_records:
            f.seek(folder['offset'])

            folder_name = ""
            if has_dir_names:
                len_byte = f.read(1)
                if len_byte:
                    name_len = struct.unpack('B', len_byte)[0]
                    folder_bytes = f.read(name_len)
                    folder_name = clean_str(folder_bytes)

            for _ in range(folder['count']):
                rec = f.read(16)
                if len(rec) < 16:
                    break
                file_hash, size, file_offset = struct.unpack('<QII', rec)
                file_entries.append({
                    'folder': folder_name,
                    'size': size,
                    'offset': file_offset
                })

        has_file_names = bool(archive_flags & FLAG_HAS_FILES)
        if has_file_names and total_file_name_len > 0:
            names_offset = 36 + (16 * folder_count) + total_folder_name_len + (16 * file_count)
            f.seek(names_offset)
            names_raw = f.read(total_file_name_len)
            file_names = [clean_str(n) for n in names_raw.split(b'\x00')]
            
            # Filter out empty strings resulting from split while keeping file alignment
            non_empty_names = [n for n in file_names if n]
            
            for i, entry in enumerate(file_entries):
                if i < len(non_empty_names):
                    entry['filename'] = non_empty_names[i]
                else:
                    entry['filename'] = f"file_{i}.bin"
        else:
            for i, entry in enumerate(file_entries):
                entry['filename'] = f"file_{i}.bin"

        extracted_count = 0
        default_compressed = bool(archive_flags & FLAG_COMPRESSED)
        embed_names = bool(archive_flags & FLAG_EMBED_NAMES)

        for entry in file_entries:
            safe_folder = clean_str(entry['folder'].encode('ascii', errors='ignore'))
            safe_file = clean_str(entry['filename'].encode('ascii', errors='ignore'))
            
            rel_path = os.path.join(safe_folder, safe_file)
            dest_path = os.path.join(output_dir, os.path.normpath(rel_path))
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)

            size_val = entry['size']
            file_offset = entry['offset']

            toggle_compression = bool(size_val & 0x40000000)
            is_compressed = default_compressed ^ toggle_compression
            real_size = size_val & 0x3FFFFFFF

            f.seek(file_offset)

            prefix_bytes = 0
            if embed_names:
                len_byte = f.read(1)
                if len_byte:
                    name_len = struct.unpack('B', len_byte)[0]
                    f.seek(name_len, os.SEEK_CUR)
                    prefix_bytes = 1 + name_len

            try:
                if is_compressed:
                    size_header = f.read(4)
                    if len(size_header) < 4:
                        continue
                    uncompressed_size = struct.unpack('<I', size_header)[0]
                    data_len = real_size - prefix_bytes - 4
                    compressed_data = f.read(data_len)
                    if version == 105:
                        data = lz4.block.decompress(compressed_data, uncompressed_size=uncompressed_size)
                    else:
                        data = zlib.decompress(compressed_data)
                else:
                    data_len = real_size - prefix_bytes
                    data = f.read(data_len)

                with open(dest_path, 'wb') as out_f:
                    out_f.write(data)
                extracted_count += 1

            except Exception as e:
                print(f"  ! Skipping {rel_path}: extraction failed ({e})")
                continue

        return extracted_count


def main(argv):
    if len(argv) < 3:
        print("Usage: python3 extract_bsa.py <output_directory> <archive1.bsa> [archive2.bsa ...]")
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
        except Exception as e:
            print(f"Error extracting {archive}: {e}")
            failed.append(archive)
            continue

        if count is None:
            failed.append(archive)
        else:
            total += count
            print(f"Extracted {count} files from {os.path.basename(archive)}.")
        sys.stdout.flush()

    print(
        f"\nDone. {total} files extracted from "
        f"{len(archives) - len(failed)}/{len(archives)} archive(s) to '{output_dir}'."
    )
    if failed:
        print("Failed archives:")
        for a in failed:
            print(f"  - {a}")
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
