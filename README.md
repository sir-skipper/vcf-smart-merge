# vcf-smart-merge
Conservative local-first vCard/VCF merge tool for combining contact exports without overwriting baseline data.

## Features

- Uses the first VCF as the authoritative baseline
- Merges contacts from multiple VCF sources
- Preserves baseline data unchanged
- Adds missing contacts
- Adds missing fields to matched contacts
- Avoids duplicate values across different field types
- Supports custom fields
- Generates merge reports

## Installation

Requires Python 3.8+.

Install dependency:

```bash
python -m pip install vobject
```

## Usage

The first VCF file is treated as the authoritative baseline.

Additional VCF files are used to enrich the baseline with missing contacts and missing fields.

```bash
python contacts_merge.py baseline.vcf source1.vcf source2.vcf
```

Example:

```bash
python contacts_merge.py nextcloud_contacts.vcf iCloud_contacts.vcf "Local Contacts.vcf"
```

## Outputs

The script generates:

```text
merged_contacts.vcf
missing_contacts.json
merge_report.txt
```

### merged_contacts.vcf

The final merged contact list.

### missing_contacts.json

Contacts that were not found in the baseline and were added during the merge.

### merge_report.txt

Human-readable report containing merge statistics and field summaries.

## Merge Philosophy

- The first VCF file is the authoritative baseline.
- Existing baseline values are preserved.
- Missing contacts are added.
- Missing fields are added to matched contacts.
- Duplicate values are ignored.
- Original contact values are preserved.
- Multiple source VCF files are supported.

## Notes

Always keep backups of your original VCF files before performing a merge.

The tool was originally developed to consolidate contact databases from sources such as:

- Nextcloud
- iCloud
- iPhone Contacts exports
- CardDAV exports
- Generic VCF exports

## License

MIT
