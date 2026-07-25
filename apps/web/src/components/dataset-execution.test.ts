import {
  DATASET_CLASSIFICATION,
  MAX_INLINE_DATASET_BYTES,
  assessDatasetFile,
  buildDatasetRunInputs,
  datasetRunDisabled,
} from "./dataset-execution";

const csv = { name: "study.csv", size: 42, type: "text/csv" };

describe("dataset execution policy", () => {
  it.each([
    [{ ...csv }, "csv"],
    [{ name: "study.json", size: 42, type: "application/json" }, "json"],
    [{ name: "study", size: 42, type: "text/csv" }, "csv"],
    [{ name: "study", size: 42, type: "application/json" }, "json"],
  ] as const)("classifies supported files", (file, kind) => {
    expect(assessDatasetFile(file)).toEqual({ kind, error: null });
  });

  it("rejects unsupported and oversized files with actionable guidance", () => {
    expect(
      assessDatasetFile({ name: "study.xlsx", size: 42, type: "application/octet-stream" }),
    ).toEqual({
      kind: null,
      error: "Only .csv or .json files are supported here.",
    });
    expect(
      assessDatasetFile({
        ...csv,
        size: MAX_INLINE_DATASET_BYTES + 1,
      }),
    ).toEqual({
      kind: null,
      error:
        "Direct Code Interpreter input is limited to 100 KB in this workspace. Larger assets need the estimate-and-approve path.",
    });
  });

  it.each([
    {
      running: true,
      planApproved: true,
      assetMode: "sample",
      uploadedFile: null,
      fileKind: null,
      csvText: null,
    },
    {
      running: false,
      planApproved: false,
      assetMode: "sample",
      uploadedFile: null,
      fileKind: null,
      csvText: null,
    },
    {
      running: false,
      planApproved: true,
      assetMode: "upload",
      uploadedFile: null,
      fileKind: null,
      csvText: null,
    },
    {
      running: false,
      planApproved: true,
      assetMode: "upload",
      uploadedFile: csv,
      fileKind: "json",
      csvText: null,
    },
    {
      running: false,
      planApproved: true,
      assetMode: "upload",
      uploadedFile: csv,
      fileKind: "csv",
      csvText: " ",
    },
  ] as const)("blocks unsafe or incomplete runs", (options) => {
    expect(datasetRunDisabled(options)).toBe(true);
  });

  it("allows approved built-in and loaded CSV runs", () => {
    expect(
      datasetRunDisabled({
        running: false,
        planApproved: true,
        assetMode: "large",
        uploadedFile: null,
        fileKind: null,
        csvText: null,
      }),
    ).toBe(false);
    expect(
      datasetRunDisabled({
        running: false,
        planApproved: true,
        assetMode: "upload",
        uploadedFile: csv,
        fileKind: "csv",
        csvText: "a,b\n1,2\n",
      }),
    ).toBe(false);
  });

  it("builds explicit classified inputs for every supported path", () => {
    expect(
      buildDatasetRunInputs({
        assetMode: "sample",
        uploadedFile: null,
        fileKind: null,
        csvText: null,
        planApproved: true,
      }),
    ).toEqual({
      compute_adapter_configured: true,
      analysis_approved: true,
      data_classification: DATASET_CLASSIFICATION,
      filename: "pilot-outcomes.csv",
      estimated_bytes: 4_000_000,
    });
    expect(
      buildDatasetRunInputs({
        assetMode: "large",
        uploadedFile: null,
        fileKind: null,
        csvText: null,
        planApproved: true,
      }),
    ).toEqual({
      compute_adapter_configured: true,
      analysis_approved: true,
      data_classification: DATASET_CLASSIFICATION,
      filename: "clinical-events-archive.parquet",
      estimated_bytes: 1_200_000_000_000,
    });
    expect(
      buildDatasetRunInputs({
        assetMode: "upload",
        uploadedFile: csv,
        fileKind: "csv",
        csvText: "a,b\n1,2\n",
        planApproved: true,
      }),
    ).toEqual({
      compute_adapter_configured: true,
      analysis_approved: true,
      data_classification: DATASET_CLASSIFICATION,
      filename: "study.csv",
      estimated_bytes: 42,
      csv_text: "a,b\n1,2\n",
    });
  });

  it("refuses to construct uploaded analysis inputs without a loaded CSV", () => {
    for (const options of [
      { uploadedFile: null, fileKind: null, csvText: null },
      { uploadedFile: csv, fileKind: "json" as const, csvText: null },
      { uploadedFile: csv, fileKind: "csv" as const, csvText: " " },
    ]) {
      expect(() =>
        buildDatasetRunInputs({
          assetMode: "upload",
          planApproved: true,
          ...options,
        }),
      ).toThrow("A loaded CSV file is required");
    }
  });
});
