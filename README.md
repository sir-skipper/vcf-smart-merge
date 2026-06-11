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
- Generates a list of newly-added contacts

## Installation

Requires Python 3.8+.

Install dependency:

```bash
python -m pip install vobject