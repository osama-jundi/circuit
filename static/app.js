/* app.js — fetches the graph data from the Flask server and draws it. */

// Tell Cytoscape we want to use the dagre layout plugin.
cytoscape.use(cytoscapeDagre);

/* Step 1: Ask the server for the graph data */
fetch("/api/graph")
  .then(r => r.json())
  .then(data => {
    // Hide the "Loading…" text
    document.getElementById("loading").style.display = "none";

    /* Step 2: Build the Cytoscape instance */
    const cy = cytoscape({
      container: document.getElementById("cy"),

      // The actual graph data (nodes + edges)
      elements: [
        ...data.elements.nodes,
        ...data.elements.edges,
      ],

      // Styling rules - similar to CSS, but for graph elements
      style: [
        // Panel boxes
        {
          selector: "node",
          style: {
            "shape": "round-rectangle",
            "background-color": "#ffffff",
            "border-color": "#1e3a5f",
            "border-width": 1.5,
            "label": "data(label)",
            "text-valign": "center",
            "text-halign": "center",
            "text-wrap": "wrap",
            "text-max-width": "120px",
            "font-size": "9px",
            "font-family": "-apple-system, Segoe UI, sans-serif",
            "color": "#1e3a5f",
            "padding": "8px",
            "width": "120px",
            "height": "28px",
          }
        },

        // Root panels (transformers, main incomers) - slightly larger, blue header
        {
          selector: "node[level = 0]",
          style: {
            "background-color": "#e7eef7",
            "border-color": "#1e3a5f",
            "border-width": 2,
            "font-weight": "bold",
          }
        },

        // Feeder lines - default look
        {
          selector: "edge",
          style: {
            "width": 2,
            "line-color": "data(color)",   // color comes from the status
            "target-arrow-color": "data(color)",
            "target-arrow-shape": "triangle",
            "arrow-scale": 0.8,
            "curve-style": "bezier",
            // Curve multiple edges between same two nodes (multi-feeders!)
            "control-point-step-size": 35,
          }
        },

        // 'Not Issued' feeders use dashed lines, like in the PDF
        {
          selector: 'edge[status = "Not Issued"]',
          style: {
            "line-style": "dashed",
            "line-dash-pattern": [6, 4],
          }
        },
      ],

      /* Step 3: Layout - dagre arranges hierarchically top-to-bottom */
      layout: {
        name: "dagre",
        rankDir: "TB",          // Top-to-Bottom
        nodeSep: 30,            // horizontal gap between siblings
        rankSep: 90,            // vertical gap between levels
        edgeSep: 12,
        // 'fit' here is unreliable because dagre runs async. We fit ourselves below.
      },

      // Interaction settings
      minZoom: 0.1,
      maxZoom: 3,
      wheelSensitivity: 0.2,
    });

    /* Step 4: Wait for the layout to finish, THEN fit to viewport.
       We also fit again after 200ms as a belt-and-braces measure -
       sometimes the very first fit happens before the container has
       its final size. */
    cy.on("layoutstop", () => {
      cy.fit(undefined, 40);
      setTimeout(() => cy.fit(undefined, 40), 200);
    });

    /* Step 5: Quick sanity log so we can confirm everything loaded */
    console.log(
      `Map ready: ${cy.nodes().length} panels, ${cy.edges().length} feeders.`
    );

    // Make 'cy' accessible from the browser console — handy for debugging
    window.cy = cy;
  })
  .catch(err => {
    document.getElementById("loading").textContent =
      "Failed to load map: " + err.message;
    console.error(err);
  });
