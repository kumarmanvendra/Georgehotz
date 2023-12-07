#!/bin/bash

# find all Python files in the staging area using git diff
# and filter for Added or Modified files (AM) that match the "*.py" pattern
git diff --cached --name-only --diff-filter=AM | grep '\.py$' | while read -r file; do
    # Strip trailing whitespace from the file
    sed -i '' 's/ *$//' "$file"
    # Stage the modified file
    git add "$file"
done
