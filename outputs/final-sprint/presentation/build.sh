#!/bin/sh
set -eu
cd "$(dirname "$0")"
mkdir -p build
pdflatex -no-shell-escape -halt-on-error -interaction=batchmode -output-directory=build final-sprint.tex
pdflatex -no-shell-escape -halt-on-error -interaction=batchmode -output-directory=build final-sprint.tex
cp build/final-sprint.pdf final-sprint.pdf
