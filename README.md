# Stage 3a — NHM Feeders SLD Web App

## What's in here

```
sld_app/
├── app.py                       <- Flask web server
├── graph.py                     <- Data loading & graph building (uses Stage 2 logic)
├── templates/
│   └── index.html               <- The page you see in the browser
├── static/
│   ├── app.js                   <- Map drawing & interactions (just drawing for now)
│   ├── cytoscape.min.js         <- Graph library (bundled, works offline)
│   ├── dagre.min.js             <- Hierarchical layout algorithm
│   └── cytoscape-dagre.js       <- Glue between them
└── README.md
```

## To run

### 1. Install Flask (one time)
```bash
pip install flask pandas openpyxl networkx
```

### 2. Put your xlsx next to app.py
The file must be named exactly `NHM_-_Feeders_Energization_OS.xlsx`.
(You can change the filename at the top of `app.py` if needed.)

### 3. Start the server
```bash
cd sld_app
python app.py
```

You should see:
```
Loaded 70 panels, 70 feeders.
 * Running on http://127.0.0.1:5000
```

### 4. Open your browser
Go to **http://127.0.0.1:5000**

You should see the map with:
- Boxes for each panel, arranged top-to-bottom by hierarchy
- Green lines for energized feeders
- Yellow lines for issued
- Grey dashed lines for not issued
- Multiple curves between same panels (e.g. 3 to BUGGY CART)

### 5. Stop the server
Press `Ctrl+C` in the terminal.

## What works now (Stage 3a)
- View the full SLD
- Zoom (scroll wheel)
- Pan (drag empty space)
- See status by line color
- Multi-feeders draw separately

## What's coming next (Stage 3b)
- Click a panel to see what connects to it
- Search by Paulos
- Click a feeder to change its status

## Stage 3c (last)
- Export button to save changes back to xlsx
