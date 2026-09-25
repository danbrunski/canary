import { useState } from "react";

import { Panel } from "../components/Panel";
import { uploadModelArtifact } from "../api/http";
import type { RunAction } from "../hooks/useAsyncAction";
import { buttonClass, inputClass, selectClass } from "../styles";
import { fileToBase64 } from "../util/browser";
import { parseJsonObject } from "../util/json";

interface ModelUploadCardProps {
  token: string;
  busy: string | null;
  run: RunAction;
}

/** Manual model-artifact + manifest upload, extracted from ControlPanel's
 *  god-panel (Task 3.2). */
export function ModelUploadCard({ token, busy, run }: ModelUploadCardProps) {
  const [modelType, setModelType] = useState("isolation_forest");
  const [artifactFile, setArtifactFile] = useState<File | null>(null);
  const [manifestFile, setManifestFile] = useState<File | null>(null);

  const uploadModel = () =>
    run("model upload", async () => {
      if (!artifactFile || !manifestFile) {
        throw new Error("Select both an artifact and manifest file.");
      }
      const artifactB64 = await fileToBase64(artifactFile);
      const manifest = parseJsonObject(await manifestFile.text());
      await uploadModelArtifact(token, modelType, artifactB64, manifest);
    });

  return (
    <Panel title="Model Upload">
      <div className="grid grid-cols-1 gap-2">
        <select
          className={selectClass}
          value={modelType}
          onChange={(event) => setModelType(event.target.value)}
        >
          <option value="isolation_forest">isolation_forest</option>
          <option value="lstm_autoencoder">lstm_autoencoder</option>
          <option value="tcn_autoencoder">tcn_autoencoder</option>
        </select>
        <input
          className={inputClass}
          type="file"
          onChange={(event) => setArtifactFile(event.target.files?.[0] ?? null)}
        />
        <input
          className={inputClass}
          type="file"
          accept="application/json,.json"
          onChange={(event) => setManifestFile(event.target.files?.[0] ?? null)}
        />
        <button
          className={buttonClass}
          type="button"
          onClick={uploadModel}
          disabled={!token || !!busy}
        >
          Publish
        </button>
      </div>
    </Panel>
  );
}
