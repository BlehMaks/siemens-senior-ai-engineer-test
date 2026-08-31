import fs from "node:fs/promises";
import path from "node:path";
import { Presentation, PresentationFile } from "@oai/artifact-tool";

const repoRoot = process.env.SIEMENS_REPO_ROOT || process.cwd();
const outputPath = process.argv[2] || path.join(
  repoRoot,
  "docs/presentation/siemens-senior-ai-engineer-overview.pptx",
);
const qaDir = process.env.SIEMENS_DECK_QA_DIR || path.join(
  path.dirname(outputPath),
  "rendered",
);

const COLORS = {
  canvas: "#FFFFFF",
  ink: "#000000",
  muted: "#5E6470",
  panel: "#EDEDED",
  rule: "#B8BCC4",
  accent: "#6DCBF4",
  accentStrong: "#3D8DFF",
  baselineFill: "#EAF3FF",
  baselineInk: "#173A7A",
  extensionFill: "#EAF8EF",
  extensionInk: "#12613B",
  warningFill: "#FFF1F0",
  warningInk: "#8C2F1E",
};

async function readJson(relativePath) {
  return JSON.parse(await fs.readFile(path.join(repoRoot, relativePath), "utf8"));
}

async function readBytes(relativePath) {
  const bytes = await fs.readFile(path.join(repoRoot, relativePath));
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
}

function addText(slide, text, position, options = {}) {
  const shape = slide.shapes.add({
    geometry: "textbox",
    name: options.name,
    position,
    fill: options.fill || "none",
    line: options.line || { style: "solid", fill: "none", width: 0 },
  });
  shape.text = text;
  shape.text.style = {
    fontSize: options.fontSize || 22,
    typeface: "Arial",
    color: options.color || COLORS.ink,
    bold: options.bold || false,
    alignment: options.alignment || "left",
    verticalAlignment: options.verticalAlignment || "top",
    autoFit: options.autoFit || "shrinkText",
  };
  return shape;
}

function addPanel(slide, position, options = {}) {
  return slide.shapes.add({
    geometry: "rect",
    name: options.name,
    position,
    fill: options.fill || COLORS.panel,
    line: {
      style: "solid",
      fill: options.lineFill || options.fill || COLORS.panel,
      width: options.lineWidth || 1,
    },
  });
}

function addRule(slide, left, top, width, fill = COLORS.rule) {
  slide.shapes.add({
    geometry: "rect",
    position: { left, top, width, height: 2 },
    fill,
    line: { style: "solid", fill, width: 0 },
  });
}

function addSlideTitle(slide, title, kicker, page) {
  addText(slide, kicker, { left: 56, top: 35, width: 400, height: 28 }, {
    fontSize: 18,
    bold: true,
    color: COLORS.muted,
  });
  addText(slide, title, { left: 56, top: 70, width: 1130, height: 72 }, {
    fontSize: 48,
    bold: true,
    autoFit: "none",
  });
  addText(slide, String(page).padStart(2, "0"), {
    left: 1180,
    top: 665,
    width: 46,
    height: 24,
  }, { fontSize: 16, alignment: "right", color: COLORS.muted });
}

function addMetric(slide, value, label, left, top, width, color = COLORS.ink) {
  addText(slide, value, { left, top, width, height: 72 }, {
    fontSize: 54,
    bold: true,
    color,
    autoFit: "none",
  });
  addText(slide, label, { left, top: top + 70, width, height: 64 }, {
    fontSize: 20,
    color: COLORS.muted,
  });
}

function setSources(slide, sources, presenterNotes = "") {
  const noteLines = [
    presenterNotes,
    "[Sources]",
    ...sources.map((source) => `- ${source}`),
  ].filter(Boolean);
  slide.speakerNotes.textFrame.setText(noteLines.join("\n"));
  slide.speakerNotes.setVisible(true);
}

async function addDiagram(slide, relativePath, position) {
  slide.images.add({
    blob: await readBytes(relativePath),
    contentType: "image/png",
    alt: `Architecture diagram from ${relativePath}`,
    fit: "contain",
    position,
  });
}

function addEvidenceBoundary(slide, text, position) {
  addPanel(slide, position, {
    fill: COLORS.warningFill,
    lineFill: "#D14B2A",
    lineWidth: 2,
  });
  addText(slide, text, {
    left: position.left + 22,
    top: position.top + 18,
    width: position.width - 44,
    height: position.height - 36,
  }, { fontSize: 19, color: COLORS.warningInk, bold: true });
}

async function build() {
  const task1Protocol = await readJson("task-01-search-agent/benchmarks/protocol.json");
  const task1Synthetic = await readJson("task-01-search-agent/benchmarks/fixtures/synthetic-report.json");
  const task2Contract = await readJson("task-02-agent-api/tests/snapshots/openapi_contract.json");
  const task3Capacity = await readJson("task-03-deployment-strategy/architecture/capacity-load-proof.sample.json");
  const task4Metrics = await readJson("task-04-binary-classification/reports/metrics.json");
  const task4Extension = await readJson("task-04-binary-classification/reports/baseline-vs-extension.json");
  const task5Metrics = await readJson("task-05-material-similarity/reports/relevance-metrics.json");
  const task5Extension = await readJson("task-05-material-similarity/reports/baseline-vs-extension.json");
  const task6Extension = await readJson("task-06-category-consolidation/reports/baseline-vs-extension.json");

  const task2OperationCount = Object.values(task2Contract.paths)
    .reduce((sum, methods) => sum + methods.length, 0);
  const task5Selected = task5Metrics.evaluations.find(
    (entry) => entry.word_weight === task5Metrics.selected_word_weight
      && entry.character_weight === task5Metrics.selected_character_weight,
  );

  const presentation = Presentation.create({ slideSize: { width: 1280, height: 720 } });

  // 1 — Minimal cover, modeled on Codex Grid slide 01.
  {
    const slide = presentation.slides.add();
    slide.background.fill = COLORS.canvas;
    addText(slide, "SIEMENS · SENIOR AI ENGINEER", {
      left: 56, top: 46, width: 560, height: 34,
    }, { fontSize: 20, bold: true, color: COLORS.muted });
    addText(slide, "Six bounded systems.\nOne evidence discipline.", {
      left: 56, top: 180, width: 1020, height: 230,
    }, { fontSize: 72, bold: true, autoFit: "none", verticalAlignment: "bottom" });
    addRule(slide, 56, 470, 112, COLORS.accentStrong);
    addText(slide, "Assignment baselines stay usable; business extensions remain explicit, tested, and reviewable.", {
      left: 56, top: 500, width: 780, height: 95,
    }, { fontSize: 28, color: COLORS.muted });
    setSources(slide, ["README.md", "docs/extension-scope-freeze.md"]);
  }

  // 2 — Cumulative narrative overview.
  {
    const slide = presentation.slides.add();
    slide.background.fill = COLORS.canvas;
    addSlideTitle(slide, "The repository turns six requirements into auditable decisions", "SYSTEM MAP", 2);
    addRule(slide, 70, 290, 1110);
    const stages = [
      ["FIND", "Task 1", "Bounded research\nand evidence"],
      ["OPERATE", "Tasks 2–3", "Durable API\nand GCP reference"],
      ["DECIDE", "Tasks 4–6", "Calibrated, compatible,\nleakage-safe extensions"],
    ];
    stages.forEach(([label, task, body], index) => {
      const left = 70 + index * 385;
      slide.shapes.add({
        geometry: "ellipse",
        position: { left, top: 280, width: 20, height: 20 },
        fill: index === 2 ? COLORS.accentStrong : COLORS.ink,
        line: { style: "solid", fill: "none", width: 0 },
      });
      addText(slide, label, { left, top: 210, width: 220, height: 38 }, {
        fontSize: 22, bold: true, color: COLORS.muted,
      });
      addText(slide, task, { left, top: 335, width: 300, height: 52 }, {
        fontSize: 30, bold: true,
      });
      addText(slide, body, { left, top: 400, width: 300, height: 130 }, {
        fontSize: 24, color: COLORS.muted,
      });
    });
    addText(slide, "Evidence moves forward; unsupported claims stop at the boundary.", {
      left: 70, top: 590, width: 1000, height: 50,
    }, { fontSize: 25, bold: true, color: COLORS.extensionInk });
    setSources(slide, ["docs/architecture.md", "docs/adr/0007-business-extension-boundaries.md"]);
  }

  // 3 — Task 1.
  {
    const slide = presentation.slides.add();
    slide.background.fill = COLORS.canvas;
    await addDiagram(slide, "docs/presentation/diagrams/task-01-search-agent.png", {
      left: 25, top: 80, width: 875, height: 505,
    });
    addText(slide, "Committed evidence", { left: 930, top: 92, width: 290, height: 44 }, {
      fontSize: 27, bold: true,
    });
    addMetric(slide, String(task1Protocol.candidates.length), "candidate model configurations in a frozen protocol", 930, 155, 285, COLORS.accentStrong);
    addMetric(slide, task1Protocol.status.toUpperCase(), "protocol status", 930, 315, 285, COLORS.ink);
    addEvidenceBoundary(slide, `Synthetic fixture: ${task1Synthetic.evidence_kind}; no live model selected.`, {
      left: 925, top: 500, width: 300, height: 130,
    });
    addText(slide, "03", { left: 1180, top: 665, width: 46, height: 24 }, { fontSize: 16, alignment: "right", color: COLORS.muted });
    setSources(slide, [
      "task-01-search-agent/benchmarks/protocol.json",
      "task-01-search-agent/benchmarks/fixtures/synthetic-report.json",
      "docs/presentation/diagrams/task-01-search-agent.mmd",
    ]);
  }

  // 4 — Task 2.
  {
    const slide = presentation.slides.add();
    slide.background.fill = COLORS.canvas;
    await addDiagram(slide, "docs/presentation/diagrams/task-02-agent-api.png", {
      left: 25, top: 80, width: 875, height: 505,
    });
    addText(slide, "Versioned contract", { left: 930, top: 92, width: 290, height: 44 }, {
      fontSize: 27, bold: true,
    });
    addMetric(slide, String(Object.keys(task2Contract.paths).length), "API paths", 930, 155, 285, COLORS.accentStrong);
    addMetric(slide, String(task2OperationCount), "HTTP operations", 930, 300, 285, COLORS.ink);
    addMetric(slide, String(task2Contract.schemas.length), "declared schemas", 930, 445, 285, COLORS.ink);
    addText(slide, "04", { left: 1180, top: 665, width: 46, height: 24 }, { fontSize: 16, alignment: "right", color: COLORS.muted });
    setSources(slide, [
      "task-02-agent-api/tests/snapshots/openapi_contract.json",
      "task-02-agent-api/README.md",
      "docs/presentation/diagrams/task-02-agent-api.mmd",
    ]);
  }

  // 5 — Task 3.
  {
    const slide = presentation.slides.add();
    slide.background.fill = COLORS.canvas;
    await addDiagram(slide, "docs/presentation/diagrams/task-03-deployment-strategy.png", {
      left: 20, top: 75, width: 880, height: 510,
    });
    addText(slide, "Local capacity proof", { left: 930, top: 90, width: 300, height: 48 }, {
      fontSize: 27, bold: true,
    });
    addMetric(slide, String(task3Capacity.scenario.submissions), "submitted jobs", 930, 155, 285, COLORS.accentStrong);
    addMetric(slide, `${task3Capacity.measurements.accepted}/${task3Capacity.scenario.submissions}`, "accepted under the bounded envelope", 930, 300, 285, COLORS.ink);
    addMetric(slide, `${task3Capacity.measurements.p95_first_event_ms.toFixed(0)} ms`, "p95 first event — fake provider", 930, 445, 285, COLORS.ink);
    addEvidenceBoundary(slide, "Reference architecture and local proof — not a live production claim.", {
      left: 55, top: 590, width: 830, height: 76,
    });
    addText(slide, "05", { left: 1180, top: 665, width: 46, height: 24 }, { fontSize: 16, alignment: "right", color: COLORS.muted });
    setSources(slide, [
      "task-03-deployment-strategy/architecture/capacity-load-proof.sample.json",
      "task-03-deployment-strategy/architecture/strategy.md",
      "docs/presentation/diagrams/task-03-deployment-strategy.mmd",
    ]);
  }

  // 6 — Task 4 architecture.
  {
    const slide = presentation.slides.add();
    slide.background.fill = COLORS.canvas;
    await addDiagram(slide, "docs/presentation/diagrams/task-04-binary-classification.png", {
      left: 22, top: 10, width: 1236, height: 700,
    });
    addText(slide, "06", { left: 1180, top: 665, width: 46, height: 24 }, { fontSize: 16, alignment: "right", color: COLORS.muted });
    setSources(slide, [
      "task-04-binary-classification/README.md",
      "docs/adr/0008-task4-calibrated-decision-layer.md",
      "docs/presentation/diagrams/task-04-binary-classification.mmd",
    ]);
  }

  // 7 — Task 4 evidence.
  {
    const slide = presentation.slides.add();
    slide.background.fill = COLORS.canvas;
    addSlideTitle(slide, "Calibration improves probability quality without rewriting the baseline", "TASK 4 · EVIDENCE", 7);
    const rawBrier = task4Extension.assignment_baseline.probability_quality.brier_score;
    const calibratedBrier = task4Extension.business_extension.calibration.holdout_probability_quality.brier_score;
    const chartBottom = 560;
    const chartScale = 920;
    for (const [tick, label] of [[0, "0.0"], [0.1, "0.1"], [0.2, "0.2"], [0.3, "0.3"]]) {
      const top = chartBottom - tick * chartScale;
      addRule(slide, 98, top, 555, COLORS.panel);
      addText(slide, label, { left: 55, top: top - 12, width: 36, height: 24 }, {
        fontSize: 12,
        alignment: "right",
        color: COLORS.muted,
      });
    }
    const bars = [
      { label: "Raw", value: rawBrier, left: 165 },
      { label: "Calibrated", value: calibratedBrier, left: 440 },
    ];
    for (const bar of bars) {
      const height = bar.value * chartScale;
      addPanel(slide, { left: bar.left, top: chartBottom - height, width: 145, height }, {
        fill: COLORS.accentStrong,
        lineFill: COLORS.accentStrong,
      });
      addText(slide, bar.value.toFixed(3), { left: bar.left, top: chartBottom - height - 30, width: 145, height: 25 }, {
        fontSize: 14,
        bold: true,
        alignment: "center",
        color: COLORS.baselineInk,
      });
      addText(slide, bar.label, { left: bar.left, top: chartBottom + 7, width: 145, height: 24 }, {
        fontSize: 13,
        alignment: "center",
        color: COLORS.muted,
      });
    }
    addText(slide, "Brier score · lower is better", { left: 95, top: 185, width: 555, height: 30 }, {
      fontSize: 17,
      bold: true,
      color: COLORS.muted,
      alignment: "center",
    });
    addText(slide, "Sanitized extension fixture · 96 train / 24 holdout", {
      left: 65, top: 590, width: 590, height: 32,
    }, { fontSize: 18, color: COLORS.muted, alignment: "center" });
    addPanel(slide, { left: 720, top: 180, width: 500, height: 355 }, { fill: COLORS.baselineFill, lineFill: COLORS.accentStrong, lineWidth: 2 });
    addText(slide, "Recorded assignment baseline", { left: 755, top: 215, width: 430, height: 44 }, {
      fontSize: 29, bold: true, color: COLORS.baselineInk,
    });
    addMetric(slide, task4Metrics.join_audit.entity_rows.toLocaleString("en-US"), "joined entities", 755, 285, 200, COLORS.baselineInk);
    addMetric(slide, task4Metrics.holdout_at_selected_threshold.pr_auc.toFixed(3), "holdout PR-AUC", 985, 285, 200, COLORS.baselineInk);
    addMetric(slide, task4Metrics.holdout_at_selected_threshold.recall.toFixed(3), "holdout recall", 755, 420, 200, COLORS.baselineInk);
    addText(slide, "Selected: weighted logistic", { left: 985, top: 445, width: 205, height: 56 }, { fontSize: 21, bold: true, color: COLORS.baselineInk });
    addEvidenceBoundary(slide, "Costs are example scenarios; class semantics and production costs require owner confirmation.", {
      left: 720, top: 560, width: 500, height: 90,
    });
    setSources(slide, [
      "task-04-binary-classification/reports/metrics.json",
      "task-04-binary-classification/reports/baseline-vs-extension.json",
    ]);
  }

  // 8 — Task 5 architecture.
  {
    const slide = presentation.slides.add();
    slide.background.fill = COLORS.canvas;
    await addDiagram(slide, "docs/presentation/diagrams/task-05-material-similarity.png", {
      left: 22, top: 10, width: 1236, height: 700,
    });
    addText(slide, "08", { left: 1180, top: 665, width: 46, height: 24 }, { fontSize: 16, alignment: "right", color: COLORS.muted });
    setSources(slide, [
      "task-05-material-similarity/README.md",
      "docs/adr/0009-task5-compatibility-policy.md",
      "docs/presentation/diagrams/task-05-material-similarity.mmd",
    ]);
  }

  // 9 — Task 5 evidence.
  {
    const slide = presentation.slides.add();
    slide.background.fill = COLORS.canvas;
    addSlideTitle(slide, "Retrieval quality and engineering safety are measured on separate evidence", "TASK 5 · EVIDENCE", 9);
    addPanel(slide, { left: 55, top: 235, width: 540, height: 330 }, { fill: COLORS.baselineFill, lineFill: COLORS.accentStrong, lineWidth: 2 });
    addText(slide, "Recorded 998-row baseline", { left: 90, top: 265, width: 470, height: 46 }, {
      fontSize: 30, bold: true, color: COLORS.baselineInk,
    });
    addMetric(slide, task5Selected.metrics.ndcg_at_5.toFixed(3), "NDCG@5", 90, 340, 200, COLORS.baselineInk);
    addMetric(slide, task5Selected.metrics.coverage.toFixed(3), "coverage", 340, 340, 200, COLORS.baselineInk);
    addText(slide, "25% word · 75% character TF-IDF", { left: 90, top: 495, width: 440, height: 50 }, { fontSize: 23, bold: true, color: COLORS.baselineInk });

    addPanel(slide, { left: 640, top: 235, width: 580, height: 330 }, { fill: COLORS.extensionFill, lineFill: "#14804A", lineWidth: 2 });
    addText(slide, "Sanitized extension fixture", { left: 680, top: 265, width: 500, height: 46 }, {
      fontSize: 30, bold: true, color: COLORS.extensionInk,
    });
    addMetric(slide, `${task5Extension.business_extension.safety_benchmark.passed_count}/${task5Extension.business_extension.safety_benchmark.case_count}`, "reviewed safety cases passed", 680, 340, 210, COLORS.extensionInk);
    addMetric(slide, "0 → 1", "strict → relaxed exactly-five coverage", 930, 340, 230, COLORS.extensionInk);
    addText(slide, `${task5Extension.business_extension.relaxed_hybrid.review_required_case_count} cases require review · ${task5Extension.business_extension.relaxed_hybrid.reviewed_hard_negative_rate.toFixed(0)} hard-negative rate`, {
      left: 680, top: 495, width: 490, height: 60,
    }, { fontSize: 22, bold: true, color: COLORS.extensionInk });
    addEvidenceBoundary(slide, "The relaxed result is an engineering review aid, never interchangeability certification.", {
      left: 250, top: 590, width: 780, height: 72,
    });
    setSources(slide, [
      "task-05-material-similarity/reports/relevance-metrics.json",
      "task-05-material-similarity/reports/baseline-vs-extension.json",
    ]);
  }

  // 10 — Task 6 architecture.
  {
    const slide = presentation.slides.add();
    slide.background.fill = COLORS.canvas;
    await addDiagram(slide, "docs/presentation/diagrams/task-06-category-consolidation.png", {
      left: 22, top: 10, width: 1236, height: 700,
    });
    addText(slide, "10", { left: 1180, top: 665, width: 46, height: 24 }, { fontSize: 16, alignment: "right", color: COLORS.muted });
    setSources(slide, [
      "task-06-category-consolidation/README.md",
      "docs/adr/0010-task6-optional-sklearn-adapter.md",
      "docs/presentation/diagrams/task-06-category-consolidation.mmd",
    ]);
  }

  // 11 — Task 6 and selected-release evidence.
  {
    const slide = presentation.slides.add();
    slide.background.fill = COLORS.canvas;
    addSlideTitle(slide, "Default equivalence survives the reusable transformer and reviewed aliases", "TASK 6 · RELEASE EVIDENCE", 11);
    addMetric(slide, task6Extension.assignment_baseline.single_column_output_equivalent ? "YES" : "NO", "assignment output equivalent", 65, 185, 300, COLORS.baselineInk);
    addMetric(slide, String(Object.values(task6Extension.business_extension.sklearn_checks).filter(Boolean).length), "sklearn integration checks", 430, 185, 300, COLORS.extensionInk);
    addMetric(slide, "v1 / v2", "default artifact / alias-aware artifact", 795, 185, 360, COLORS.accentStrong);
    addRule(slide, 65, 355, 1090);
    addMetric(slide, "395", "Tasks 4–6 deterministic tests passed", 65, 400, 330, COLORS.ink);
    addMetric(slide, "100%", "2,951 statements", 470, 400, 300, COLORS.ink);
    addMetric(slide, "100%", "812 branches", 830, 400, 300, COLORS.ink);
    addText(slide, "3 owner-private-data tests remain explicit acceptance handoffs.", {
      left: 65, top: 595, width: 900, height: 42,
    }, { fontSize: 24, bold: true, color: COLORS.warningInk });
    setSources(slide, [
      "task-06-category-consolidation/reports/baseline-vs-extension.json",
      "reports/test-coverage-baseline.md",
    ]);
  }

  // 12 — Close on the decision and limits, not a generic thank-you.
  {
    const slide = presentation.slides.add();
    slide.background.fill = COLORS.canvas;
    addText(slide, "RELEASE POSITION", { left: 56, top: 50, width: 400, height: 32 }, {
      fontSize: 20, bold: true, color: COLORS.muted,
    });
    addText(slide, "Ready for owner evidence,\nwithout overstating it.", {
      left: 56, top: 150, width: 850, height: 180,
    }, { fontSize: 62, bold: true, autoFit: "none" });
    addRule(slide, 56, 375, 112, COLORS.accentStrong);
    addText(slide, "Deterministic release", { left: 56, top: 420, width: 320, height: 38 }, { fontSize: 25, bold: true, color: COLORS.extensionInk });
    addText(slide, "Tasks 4–6 code, reports, docs, and presentation evidence are locally reproducible.", {
      left: 56, top: 470, width: 500, height: 105,
    }, { fontSize: 23, color: COLORS.muted });
    addText(slide, "Owner acceptance", { left: 650, top: 420, width: 320, height: 38 }, { fontSize: 25, bold: true, color: COLORS.warningInk });
    addText(slide, "Private datasets, second-computer setup, and live deployment remain explicit owner-run checks.", {
      left: 650, top: 470, width: 500, height: 105,
    }, { fontSize: 23, color: COLORS.muted });
    addText(slide, "12", { left: 1180, top: 665, width: 46, height: 24 }, { fontSize: 16, alignment: "right", color: COLORS.muted });
    setSources(slide, ["docs/owner-acceptance-checklist.md", "docs/reviewer-guide.md"]);
  }

  await fs.mkdir(path.dirname(outputPath), { recursive: true });
  await fs.mkdir(qaDir, { recursive: true });
  for (const [index, slide] of presentation.slides.items.entries()) {
    const stem = `slide-${String(index + 1).padStart(2, "0")}`;
    const png = await presentation.export({ slide, format: "png", scale: 1 });
    await fs.writeFile(path.join(qaDir, `${stem}.png`), new Uint8Array(await png.arrayBuffer()));
    const layout = await slide.export({ format: "layout" });
    await fs.writeFile(path.join(qaDir, `${stem}.layout.json`), await layout.text());
  }
  const montage = await presentation.export({ format: "webp", montage: true, scale: 1 });
  await fs.writeFile(path.join(qaDir, "deck-montage.webp"), new Uint8Array(await montage.arrayBuffer()));
  const pptx = await PresentationFile.exportPptx(presentation);
  await pptx.save(outputPath);
}

build().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
