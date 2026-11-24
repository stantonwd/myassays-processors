# myassays-processors
Extract data on recovery curves, controls, and sample binding from MyAssays .xlsx output

## Run

On Windows, double-click `run_app.bat`. The first time, it creates a `.venv` with
the required packages; after that it just starts the app in your browser.

Or by hand:

```
pip install -r requirements.txt
streamlit run app.py
```

## Where data is saved

Real data never goes in this repository. The app reads and writes one **data folder**:

```
<data folder>/
  fecal_hormone_collection_YYYYMMDD.csv   newest one is used for dry weight + extraction volume
  records/
    plates.csv            one row per plate
    assays.csv            one row per sample per run (run_id = plate_id_wells)
    controls.csv          one row per control per plate (keep_yn = used in CVs)
    hormone_results.csv   one reported value per sample x hormone, with final_conc_pg_g
```

Set the folder in `.streamlit/secrets.toml` (not committed; copy
`.streamlit/secrets.toml.example`), or with the `MYASSAYS_DATA_DIR` environment
variable. Without either, `~/myassays_data` is used. "Save plate to records" in the
sidebar adds a plate; saving the same plate again replaces it. These CSVs stand in
for the SQL database until it exists.

## Pages

- **Process plate**: upload one MyAssays export; tabs Curve, Samples, Controls; save
  it to the records. Each sample gets an automatic result: the latest passing run.
- **Issues**: works from the records, no upload needed. Lists samples that still need
  a decision: every run failed, a single run with binding outside the window, or a
  failed run waiting for a rerun. Pick a sample to see every duplicate read from every
  run as conc × dilution; the two closest reads are ticked to start, and the mean,
  CV and pg/g update as you tick. Saving needs your initials, plus a justification
  when reads come from a failed run or from more than one run. Test wells can be
  cleared in bulk as "not a study sample". Re-saving a plate keeps these decisions
  unless the plate adds a new run of the sample.
- **Levey-Jennings**: each control lot across plates, showing read 1, read 2 and the
  average. Click an average to accept or reject that control for the CVs.

## Reruns

A sample is marked as a rerun when it already has a run of the same hormone in the
records. A single run outside the binding window is REVIEW; if every run is outside
binding (or fails for any reason) the sample is FAIL (all runs) and goes to Issues.

final_conc_pg_g = conc × dilution (pg/mL) × extraction volume (mL) ÷ dry weight (g).
