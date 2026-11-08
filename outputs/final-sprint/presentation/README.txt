Final sprint presentation

Seven numbered slides for a two-minute talk.

The first slide cites the OS-Harm benchmark. The deck includes dataset and
result figures, a judge-agreement figure, and excerpts of the original recorded
email and draft screenshots. The screenshot crops only focus the view; the full
recorded images and GIFs remain in the assets folder.

final-sprint.pdf is the compiled presentation.
final-sprint.tex is the editable LaTeX Beamer source.
speaker-script.txt gives the narration and measured slide timings.
narration.txt contains only the spoken text.
narration.json contains the slide-by-slide text and timing data.
references.bib contains the benchmark reference.

To rebuild the PDF after editing, install a TeX distribution with Beamer and run:
sh build.sh

The recorded trace excerpts appear directly on the slides. The full GIFs and
traces are in the assets folder for reference. The PDF has no file-launch links.

The narrated MP4 and standalone MP3 are available beside this bundle in the
repository's presentation folder. The audio uses the selected Gradium voice.

The slides report the original baseline's 137 judged runs. A separate recovery
completed the remaining 13 with some setup changes. Its results are reported
separately in the project Markdown and are not merged into these figures.

GPT 6.1 Terra drafted the deck in Fast mode. The final slides received a wording
and layout review before compilation.
