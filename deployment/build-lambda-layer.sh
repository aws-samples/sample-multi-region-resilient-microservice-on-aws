#!/bin/bash
set -eo pipefail
cd database/crdr-reconciliation/src/function
rm -rf package
mkdir -p package/python
pip3 install --target ./package/python -r requirements.txt

