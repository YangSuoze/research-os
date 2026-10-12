# Algorithmic idea criteria

## Choose a meaningful delta

Connect the change to a supported failure, constraint or unresolved trade-off. Useful routes include:

- **Repair a bottleneck:** change a training objective, sampling rule, inference procedure or representation because the baseline fails under a stated condition.
- **Transfer a mechanism:** reuse a mechanism from another task, identifying which assumptions survive and which require an adaptation.
- **Combine complementary mechanisms:** explain the interaction, shared variables and interface between components, rather than placing two techniques side by side.
- **Remove unnecessary machinery:** propose a simpler mechanism while testing which behavior the removed component was responsible for.

Describe what is computed, what information it uses, and when it acts during training or inference. Use a small equation or pseudocode only when it clarifies the change. A new name, extra module, altered scalar weight or higher retrieval frequency does not by itself establish a substantive contribution.

Keep the distinction between MethodFamily (broad approach), MethodLeaf (reusable mechanism) and a paper's implementation details. Do not promote every implementation variation into a new atomic method or treat names as sufficient evidence of equality.

## Check the premises

- Verify source mechanisms and claimed limitations against their original blocks. Absence of an extracted limitation is not evidence that a method has none.
- Describe limitations with their conditions. A finding for one dataset, model size or evaluation regime is not a universal defect.
- Distinguish co-use from interaction and reported correlation from a causal claim.
- When transferring or combining methods, check required signals, available labels, training/inference access, interfaces and computational cost in the original text. If these are unknown, describe an assumption or stop claiming compatibility.
- Compare results only after checking datasets, splits, metrics and evaluation protocols. Shared Dataset identity alone does not make reported numbers comparable.

## Design the smallest informative experiment

Tie the experiment to the proposed explanation:

1. Identify a source-backed baseline and a usable dataset or specify what data is still needed. Do not invent a dataset or imply that it has been downloaded.
2. Choose the metric and a targeted stress condition that exposes the bottleneck; measure added cost where relevant.
3. Isolate the delta. For an A+B proposal, compare baseline, A-only, B-only and A+B where those variants are meaningful; use a control that tests the claimed coupling.
4. State what observation would support the explanation and what would falsify it. Separate genuine interaction from extra capacity, extra data or a changed budget.

These are proposed experiments, not completed results. Do not fabricate gains, significance, confidence scores or assurances of publication acceptance.

## Assess novelty honestly

Search the graph and relevant original text using concrete mechanism terms and grounded synonyms. A proposed combination may already exist under another name. Report a found overlap, a checked corpus with no identified overlap, or insufficient coverage. None of these establishes worldwide novelty without a broader literature check. Keep the scientific value conditional on evidence and experimental validation, not on a numerical novelty score invented by the model.
