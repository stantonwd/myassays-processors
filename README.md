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

## Reruns

A sample is marked as a rerun when it already has a run of the same hormone in the
records. On the **Reruns & results** tab:

- the latest passing run is reported by default;
- a single run outside the binding window is REVIEW; if every run is outside binding
  (or fails for any reason) the sample is FAIL (all runs);
- to report from failed runs, choose "Use run(s)" and enter run numbers (several =
  average of conc × dilution); this needs initials and a justification.

final_conc_pg_g = conc × dilution (pg/mL) × extraction volume (mL) ÷ dry weight (g).
