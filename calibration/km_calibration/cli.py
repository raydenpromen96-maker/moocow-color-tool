"""Command-line entrypoint for the isolated synthetic K-M calibration workflow."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .acquisition_preflight import (
    AcquisitionPreflightError,
    assemble_acquisition_preflight,
    commit_holdout_custody,
    prepare_acquisition_package,
    preflight_open_batches,
    preflight_pilot_materials,
    verify_acquisition_preflight,
)
from .diagnostic import (
    DiagnosticValidationError,
    bind_evidence_record,
    generate_weighing_plan_from_file,
    preflight_from_files,
    prepare_four_card,
    validate_structure_from_files,
    verify_preflight_receipt,
)
from .errors import CalibrationError
from .hashing import read_json
from .open_measurement_admission import admit_open_measurements, verify_open_measurement_admission
from .open_measurement_pack import assemble_open_measurement_input, prepare_open_measurement_pack
from .pilot import freeze_pilot_design, prepare_pilot, verify_pilot_design_receipt
from .pipeline import evaluate_model, export_candidate, fit_km, write_evaluation, write_model
from .schema import load_and_validate_dataset, split_audit
from .synthetic import generate_synthetic_dataset


def _json_output(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))


def _add_independent_holdout_authority_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--acquisition-receipt", type=Path, required=True)
    parser.add_argument("--admission-receipt", type=Path, required=True)
    parser.add_argument("--dataset-root", type=Path, required=True)
    parser.add_argument("--shared-root", type=Path, required=True)
    parser.add_argument("--open-root", type=Path, required=True)
    parser.add_argument("--measurement-root", type=Path, required=True)
    parser.add_argument("--candidate-export-root", type=Path, required=True)
    parser.add_argument("--baseline-model", type=Path, required=True)
    parser.add_argument("--custody-commitment", type=Path, required=True)
    parser.add_argument("--repeatability-receipt", type=Path, required=True)
    parser.add_argument("--acceptance-profile", type=Path, required=True)
    parser.add_argument("--colorimetry-profile", type=Path, required=True)
    parser.add_argument("--trust-store", type=Path, required=True)
    parser.add_argument("--preregistration-envelope", type=Path, required=True)


def _add_open_selection_recipe_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--acquisition-receipt", type=Path, required=True)
    parser.add_argument("--admission-receipt", type=Path, required=True)
    parser.add_argument("--dataset-root", type=Path, required=True)
    parser.add_argument("--shared-root", type=Path, required=True)
    parser.add_argument("--open-root", type=Path, required=True)
    parser.add_argument("--measurement-root", type=Path, required=True)
    parser.add_argument("--fit-export-root", type=Path, required=True)
    parser.add_argument("--request-root", type=Path, required=True)
    parser.add_argument("--request-relative-path", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="moocow-km-calibration")
    commands = parser.add_subparsers(dest="command", required=True)

    generate = commands.add_parser("generate-synthetic", help="Generate deterministic synthetic-only K-M records")
    generate.add_argument("--output", type=Path, required=True)
    generate.add_argument("--seed", type=int, default=20260714)
    generate.add_argument("--noise-std", type=float, default=0.0)

    validate = commands.add_parser("validate-dataset", help="Strictly validate hashes, schema, targets, and conditions")
    validate.add_argument("--dataset", type=Path, required=True)

    audit = commands.add_parser("audit-splits", help="Fail closed on formula-family split leakage")
    audit.add_argument("--dataset", type=Path, required=True)

    fit = commands.add_parser("fit-km", help="Bounded joint synthetic-only black/white finite-film two-constant fit")
    fit.add_argument("--dataset", type=Path, required=True)
    fit.add_argument("--output-model", type=Path, required=True)
    fit.add_argument("--model-version", default="km-synthetic-v1")
    fit.add_argument("--max-nfev", type=int, default=3000)

    evaluate = commands.add_parser("evaluate", help="Evaluate a hash-bound synthetic/research-only model")
    evaluate.add_argument("--dataset", type=Path, required=True)
    evaluate.add_argument("--model", type=Path, required=True)
    evaluate.add_argument("--output", type=Path, required=True)

    export = commands.add_parser("export-candidate", help="Write a non-promotable synthetic/research receipt")
    export.add_argument("--dataset", type=Path, required=True)
    export.add_argument("--model", type=Path, required=True)
    export.add_argument("--evaluation", type=Path, required=True)
    export.add_argument("--output-receipt", type=Path, required=True)

    prepare = commands.add_parser("prepare-four-card", help="Create an explicitly incomplete four-card diagnostic operator pack")
    prepare.add_argument("--registry", type=Path, required=True)
    prepare.add_argument("--output-dir", type=Path, required=True)

    preflight = commands.add_parser(
        "preflight-four-card", help="Fail-closed diagnostic-only four-card JSON/CSV preflight into a new empty output directory"
    )
    preflight.add_argument("--format", choices=("json", "csv"), required=True)
    preflight.add_argument("--input", type=Path, required=True)
    preflight.add_argument("--manifest", type=Path)
    preflight.add_argument("--evidence-root", type=Path)
    preflight.add_argument("--output-dir", type=Path, required=True)

    structure = commands.add_parser(
        "validate-four-card-structure", help="Validate only the v2 four-card transport without opening evidence files"
    )
    structure.add_argument("--format", choices=("json", "csv"), required=True)
    structure.add_argument("--input", type=Path, required=True)
    structure.add_argument("--manifest", type=Path)

    bind_evidence = commands.add_parser(
        "bind-evidence-record", help="Validate a portable whole-file or byte-range evidence locator"
    )
    bind_evidence.add_argument("--evidence-root", type=Path, required=True)
    bind_evidence.add_argument("--relative-path", required=True)
    bind_evidence.add_argument("--whole-file", action="store_true")
    bind_evidence.add_argument("--byte-offset", type=int)
    bind_evidence.add_argument("--byte-length", type=int)

    verify_receipt = commands.add_parser(
        "verify-four-card-receipt", help="Reverify a v2 receipt against another evidence-root copy"
    )
    verify_receipt.add_argument("--receipt", type=Path, required=True)
    verify_receipt.add_argument("--evidence-root", type=Path, required=True)

    weighing_plan = commands.add_parser(
        "generate-weighing-plan",
        help="Generate current-lot target wet masses; output is planning data, not actual weighing evidence",
    )
    weighing_plan.add_argument("--input", type=Path, required=True)
    weighing_plan.add_argument("--evidence-root", type=Path, required=True)
    weighing_plan.add_argument("--output", type=Path, required=True)

    prepare_pilot_parser = commands.add_parser("prepare-pilot", help="Create an explicitly invalid 45-card pilot acquisition pack")
    prepare_pilot_parser.add_argument("--registry", type=Path, required=True)
    prepare_pilot_parser.add_argument("--output-dir", type=Path, required=True)

    prepare_acquisition = commands.add_parser(
        "prepare-acquisition-package", help="Create a private, deliberately invalid 15/17/3 acquisition-template package"
    )
    prepare_acquisition.add_argument(
        "--conversion-route",
        choices=("mass_solids_nonvolatile_density", "wet_density_volume_solids"),
        required=True,
    )
    prepare_acquisition.add_argument("--output-dir", type=Path, required=True)

    freeze_pilot = commands.add_parser(
        "freeze-pilot-design", help="Freeze a preregistered 45-card design after rechecking the real four-card receipt"
    )
    freeze_pilot.add_argument("--design", type=Path, required=True)
    freeze_pilot.add_argument("--registry", type=Path, required=True)
    freeze_pilot.add_argument("--registry-evidence-root", type=Path, required=True)
    freeze_pilot.add_argument("--diagnostic-receipt", type=Path, required=True)
    freeze_pilot.add_argument("--diagnostic-evidence-root", type=Path, required=True)
    freeze_pilot.add_argument("--output-dir", type=Path, required=True)

    verify_pilot = commands.add_parser(
        "verify-pilot-design-receipt", help="Reverify a frozen pilot design, registry, and four-card evidence"
    )
    verify_pilot.add_argument("--receipt", type=Path, required=True)
    verify_pilot.add_argument("--design", type=Path, required=True)
    verify_pilot.add_argument("--registry", type=Path, required=True)
    verify_pilot.add_argument("--registry-evidence-root", type=Path, required=True)
    verify_pilot.add_argument("--diagnostic-receipt", type=Path, required=True)
    verify_pilot.add_argument("--diagnostic-evidence-root", type=Path, required=True)

    preflight_materials = commands.add_parser(
        "preflight-pilot-materials",
        help="Verify the frozen pilot and exactly 15 current-lot material/property records without admitting spectra",
    )
    preflight_materials.add_argument("--pilot-design-receipt", type=Path, required=True)
    preflight_materials.add_argument("--design", type=Path, required=True)
    preflight_materials.add_argument("--registry", type=Path, required=True)
    preflight_materials.add_argument("--registry-evidence-root", type=Path, required=True)
    preflight_materials.add_argument("--diagnostic-receipt", type=Path, required=True)
    preflight_materials.add_argument("--diagnostic-evidence-root", type=Path, required=True)
    preflight_materials.add_argument(
        "--shared-root", "--shared-evidence-root", dest="shared_root", type=Path, required=True
    )
    preflight_materials.add_argument("--output-dir", type=Path, required=True)

    preflight_open = commands.add_parser(
        "preflight-open-batches",
        help="Verify exactly 15 train plus 2 validation batches and emit the pre-spectra actual-NV rank receipt",
    )
    preflight_open.add_argument("--materials-receipt", type=Path, required=True)
    preflight_open.add_argument(
        "--open-batch-root", "--open-batches", dest="open_batch_root", type=Path, required=True
    )
    preflight_open.add_argument("--open-evidence-root", type=Path, required=True)
    preflight_open.add_argument("--output-dir", type=Path, required=True)

    commit_holdout = commands.add_parser(
        "commit-holdout-custody",
        help="Validate the three sealed holdout batches and publish only an aggregate custody commitment",
    )
    commit_holdout.add_argument("--materials-receipt", type=Path, required=True)
    commit_holdout.add_argument("--open-batch-receipt", type=Path, required=True)
    commit_holdout.add_argument(
        "--sealed-holdout-batch-root",
        "--sealed-holdout-batches",
        dest="sealed_holdout_batch_root",
        type=Path,
        required=True,
    )
    commit_holdout.add_argument(
        "--sealed-evidence-root", "--holdout-evidence-root", dest="sealed_evidence_root", type=Path, required=True
    )
    commit_holdout.add_argument("--custody-identity", required=True)
    commit_holdout.add_argument("--custody-key-fingerprint", required=True)
    commit_holdout.add_argument("--signature-metadata", type=Path, required=True)
    commit_holdout.add_argument("--output-dir", type=Path, required=True)

    assemble_preflight = commands.add_parser(
        "assemble-acquisition-preflight",
        help="Bind the verified open receipt and opaque holdout commitment while keeping every permission false",
    )
    assemble_preflight.add_argument("--open-batch-receipt", type=Path, required=True)
    assemble_preflight.add_argument("--holdout-custody-commitment", type=Path, required=True)
    assemble_preflight.add_argument("--output-dir", type=Path, required=True)

    verify_acquisition = commands.add_parser(
        "verify-acquisition-preflight",
        help="Reverify the final receipt against copied shared/open roots without opening sealed holdout evidence",
    )
    verify_acquisition.add_argument("--receipt", type=Path, required=True)
    verify_acquisition.add_argument(
        "--shared-root", "--shared-evidence-root", dest="shared_root", type=Path, required=True
    )
    verify_acquisition.add_argument("--open-root", type=Path, required=True)

    prepare_open_measurement_pack_parser = commands.add_parser(
        "prepare-open-measurement-pack",
        help="Create a receipt-derived, deliberately incomplete 36/72/6/216/222 operator pack",
    )
    prepare_open_measurement_pack_parser.add_argument("--acquisition-receipt", type=Path, required=True)
    prepare_open_measurement_pack_parser.add_argument("--shared-root", type=Path, required=True)
    prepare_open_measurement_pack_parser.add_argument("--open-root", type=Path, required=True)
    prepare_open_measurement_pack_parser.add_argument("--output-dir", type=Path, required=True)

    assemble_open_measurement_input_parser = commands.add_parser(
        "assemble-open-measurement-input",
        help="Assemble completed neutral operator files into existing open-measurement admission input v1",
    )
    assemble_open_measurement_input_parser.add_argument("--acquisition-receipt", type=Path, required=True)
    assemble_open_measurement_input_parser.add_argument("--shared-root", type=Path, required=True)
    assemble_open_measurement_input_parser.add_argument("--open-root", type=Path, required=True)
    assemble_open_measurement_input_parser.add_argument("--pack-root", type=Path, required=True)
    assemble_open_measurement_input_parser.add_argument("--operator-input-dir", type=Path, required=True)
    assemble_open_measurement_input_parser.add_argument("--measurement-root", type=Path, required=True)
    assemble_open_measurement_input_parser.add_argument("--output-relative-path", required=True)

    admit_open_measurements_parser = commands.add_parser(
        "admit-open-measurements",
        help="Admit source-bound open measurements into a non-promotable open-selection dataset",
    )
    admit_open_measurements_parser.add_argument("--acquisition-receipt", type=Path, required=True)
    admit_open_measurements_parser.add_argument("--shared-root", type=Path, required=True)
    admit_open_measurements_parser.add_argument("--open-root", type=Path, required=True)
    admit_open_measurements_parser.add_argument("--measurement-root", type=Path, required=True)
    admit_open_measurements_parser.add_argument("--admission-input", required=True)
    admit_open_measurements_parser.add_argument("--output-dir", type=Path, required=True)

    verify_open_measurement_admission_parser = commands.add_parser(
        "verify-open-measurement-admission",
        help="Reverify an open-measurement admission without fitting or activation",
    )
    verify_open_measurement_admission_parser.add_argument("--acquisition-receipt", type=Path, required=True)
    verify_open_measurement_admission_parser.add_argument("--admission-receipt", type=Path, required=True)
    verify_open_measurement_admission_parser.add_argument("--dataset-root", type=Path, required=True)
    verify_open_measurement_admission_parser.add_argument("--shared-root", type=Path, required=True)
    verify_open_measurement_admission_parser.add_argument("--open-root", type=Path, required=True)
    verify_open_measurement_admission_parser.add_argument("--measurement-root", type=Path, required=True)

    fit_open_selection_candidate = commands.add_parser(
        "fit-open-selection-candidate",
        help="Fit and export a receipt-gated, non-promotable open-selection K-M candidate",
    )
    fit_open_selection_candidate.add_argument("--acquisition-receipt", type=Path, required=True)
    fit_open_selection_candidate.add_argument("--admission-receipt", type=Path, required=True)
    fit_open_selection_candidate.add_argument("--dataset-root", type=Path, required=True)
    fit_open_selection_candidate.add_argument("--shared-root", type=Path, required=True)
    fit_open_selection_candidate.add_argument("--open-root", type=Path, required=True)
    fit_open_selection_candidate.add_argument("--measurement-root", type=Path, required=True)
    fit_open_selection_candidate.add_argument("--output-dir", type=Path, required=True)

    verify_open_selection_candidate_export = commands.add_parser(
        "verify-open-selection-candidate-export",
        help="Reverify a receipt-gated open-selection K-M candidate export without activation",
    )
    verify_open_selection_candidate_export.add_argument("--acquisition-receipt", type=Path, required=True)
    verify_open_selection_candidate_export.add_argument("--admission-receipt", type=Path, required=True)
    verify_open_selection_candidate_export.add_argument("--dataset-root", type=Path, required=True)
    verify_open_selection_candidate_export.add_argument("--shared-root", type=Path, required=True)
    verify_open_selection_candidate_export.add_argument("--open-root", type=Path, required=True)
    verify_open_selection_candidate_export.add_argument("--measurement-root", type=Path, required=True)
    verify_open_selection_candidate_export.add_argument("--export-root", type=Path, required=True)

    solve_open_selection_recipe_candidate_parser = commands.add_parser(
        "solve-open-selection-recipe-candidate",
        help="Export a deterministic open-selection laboratory-trial recipe candidate",
    )
    _add_open_selection_recipe_arguments(solve_open_selection_recipe_candidate_parser)
    solve_open_selection_recipe_candidate_parser.add_argument("--output-dir", type=Path, required=True)

    verify_open_selection_recipe_candidate_parser = commands.add_parser(
        "verify-open-selection-recipe-candidate",
        help="Reconstruct an inactive open-selection laboratory-trial recipe candidate",
    )
    _add_open_selection_recipe_arguments(verify_open_selection_recipe_candidate_parser)
    verify_open_selection_recipe_candidate_parser.add_argument("--candidate-root", type=Path, required=True)

    verify_holdout_preregistration = commands.add_parser(
        "verify-holdout-preregistration",
        help="Verify signed independent-holdout preregistration without opening sealed input",
    )
    _add_independent_holdout_authority_arguments(verify_holdout_preregistration)

    run_independent_holdout_evaluation = commands.add_parser(
        "run-independent-holdout-evaluation",
        help="Run a software-validation-only synthetic holdout review package; measured authority remains unavailable",
    )
    _add_independent_holdout_authority_arguments(run_independent_holdout_evaluation)
    run_independent_holdout_evaluation.add_argument("--release-envelope", type=Path, required=True)
    run_independent_holdout_evaluation.add_argument("--sealed-input", type=Path, required=True)
    run_independent_holdout_evaluation.add_argument("--output-dir", type=Path, required=True)
    run_independent_holdout_evaluation.add_argument("--release-ledger-dir", type=Path)

    verify_independent_holdout_evaluation = commands.add_parser(
        "verify-independent-holdout-evaluation",
        help="Reconstruct and reverify a software-validation-only synthetic review package",
    )
    _add_independent_holdout_authority_arguments(verify_independent_holdout_evaluation)
    verify_independent_holdout_evaluation.add_argument("--release-envelope", type=Path, required=True)
    verify_independent_holdout_evaluation.add_argument("--sealed-input", type=Path, required=True)
    verify_independent_holdout_evaluation.add_argument("--evaluation-root", type=Path, required=True)
    verify_independent_holdout_evaluation.add_argument("--release-ledger-dir", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "generate-synthetic":
            _json_output(generate_synthetic_dataset(args.output, seed=args.seed, noise_std=args.noise_std))
        elif args.command == "validate-dataset":
            dataset = load_and_validate_dataset(args.dataset)
            _json_output(
                {
                    "status": "pass",
                    "dataset_status": dataset.dataset_status,
                    "manifest_sha256": dataset.manifest_sha256,
                    "records": len(dataset.records),
                }
            )
        elif args.command == "audit-splits":
            _json_output(split_audit(load_and_validate_dataset(args.dataset)))
        elif args.command == "fit-km":
            dataset = load_and_validate_dataset(args.dataset)
            outcome = fit_km(dataset, model_version=args.model_version, max_nfev=args.max_nfev)
            model_sha256 = write_model(args.output_model, outcome.model)
            _json_output({"status": dataset.dataset_status, "model_sha256": model_sha256, "fit": outcome.metrics})
        elif args.command == "evaluate":
            dataset = load_and_validate_dataset(args.dataset)
            evaluation, _model_sha256 = evaluate_model(dataset, args.model)
            evaluation_sha256 = write_evaluation(args.output, evaluation)
            _json_output({"status": evaluation["status"], "evaluation_sha256": evaluation_sha256, "metrics": evaluation["metrics"]})
        elif args.command == "export-candidate":
            dataset = load_and_validate_dataset(args.dataset)
            receipt, receipt_sha256 = export_candidate(dataset, args.model, args.evaluation, args.output_receipt)
            _json_output({"status": receipt["status"], "production_pass": False, "receipt_sha256": receipt_sha256})
        elif args.command == "prepare-four-card":
            _json_output(prepare_four_card(args.registry, args.output_dir))
        elif args.command == "preflight-four-card":
            if args.evidence_root is None:
                raise DiagnosticValidationError("CLI_ARGUMENT", "--evidence-root", "is required")
            _json_output(
                preflight_from_files(
                    input_format=args.format,
                    input_path=args.input,
                    manifest_path=args.manifest,
                    evidence_root=args.evidence_root,
                    output_dir=args.output_dir,
                )
            )
        elif args.command == "validate-four-card-structure":
            _json_output(
                validate_structure_from_files(
                    input_format=args.format,
                    input_path=args.input,
                    manifest_path=args.manifest,
                )
            )
        elif args.command == "bind-evidence-record":
            _json_output(
                bind_evidence_record(
                    evidence_root=args.evidence_root,
                    relative_path=args.relative_path,
                    whole_file=args.whole_file,
                    byte_offset=args.byte_offset,
                    byte_length=args.byte_length,
                )
            )
        elif args.command == "verify-four-card-receipt":
            _json_output(verify_preflight_receipt(receipt_path=args.receipt, evidence_root=args.evidence_root))
        elif args.command == "generate-weighing-plan":
            _json_output(
                generate_weighing_plan_from_file(
                    input_path=args.input,
                    evidence_root=args.evidence_root,
                    output_path=args.output,
                )
            )
        elif args.command == "prepare-pilot":
            _json_output(prepare_pilot(args.registry, args.output_dir))
        elif args.command == "prepare-acquisition-package":
            _json_output(prepare_acquisition_package(conversion_route=args.conversion_route, output_dir=args.output_dir))
        elif args.command == "freeze-pilot-design":
            _json_output(
                freeze_pilot_design(
                    design_path=args.design,
                    registry_path=args.registry,
                    registry_evidence_root=args.registry_evidence_root,
                    diagnostic_receipt_path=args.diagnostic_receipt,
                    diagnostic_evidence_root=args.diagnostic_evidence_root,
                    output_dir=args.output_dir,
                )
            )
        elif args.command == "verify-pilot-design-receipt":
            _json_output(
                verify_pilot_design_receipt(
                    receipt_path=args.receipt,
                    design_path=args.design,
                    registry_path=args.registry,
                    registry_evidence_root=args.registry_evidence_root,
                    diagnostic_receipt_path=args.diagnostic_receipt,
                    diagnostic_evidence_root=args.diagnostic_evidence_root,
                )
            )
        elif args.command == "preflight-pilot-materials":
            _json_output(
                preflight_pilot_materials(
                    pilot_design_receipt_path=args.pilot_design_receipt,
                    design_path=args.design,
                    registry_path=args.registry,
                    registry_evidence_root=args.registry_evidence_root,
                    diagnostic_receipt_path=args.diagnostic_receipt,
                    diagnostic_evidence_root=args.diagnostic_evidence_root,
                    shared_root=args.shared_root,
                    output_dir=args.output_dir,
                )
            )
        elif args.command == "preflight-open-batches":
            _json_output(
                preflight_open_batches(
                    materials_receipt_path=args.materials_receipt,
                    open_batch_root=args.open_batch_root,
                    open_evidence_root=args.open_evidence_root,
                    output_dir=args.output_dir,
                )
            )
        elif args.command == "commit-holdout-custody":
            try:
                signature_metadata = read_json(args.signature_metadata)
            except CalibrationError as error:
                raise AcquisitionPreflightError(
                    "RECEIPT_BINDING", str(args.signature_metadata), str(error)
                ) from error
            if not isinstance(signature_metadata, dict):
                raise AcquisitionPreflightError(
                    "TYPE", str(args.signature_metadata), "signature metadata must be a JSON object"
                )
            _json_output(
                commit_holdout_custody(
                    materials_receipt_path=args.materials_receipt,
                    open_batch_receipt_path=args.open_batch_receipt,
                    sealed_holdout_batch_root=args.sealed_holdout_batch_root,
                    sealed_evidence_root=args.sealed_evidence_root,
                    custody_identity=args.custody_identity,
                    custody_key_fingerprint=args.custody_key_fingerprint,
                    signature_metadata=signature_metadata,
                    output_dir=args.output_dir,
                )
            )
        elif args.command == "assemble-acquisition-preflight":
            _json_output(
                assemble_acquisition_preflight(
                    open_batch_receipt_path=args.open_batch_receipt,
                    holdout_custody_commitment_path=args.holdout_custody_commitment,
                    output_dir=args.output_dir,
                )
            )
        elif args.command == "verify-acquisition-preflight":
            _json_output(
                verify_acquisition_preflight(
                    receipt_path=args.receipt,
                    shared_root=args.shared_root,
                    open_root=args.open_root,
                )
            )
        elif args.command == "prepare-open-measurement-pack":
            _json_output(
                prepare_open_measurement_pack(
                    acquisition_receipt_path=args.acquisition_receipt,
                    shared_root=args.shared_root,
                    open_root=args.open_root,
                    output_dir=args.output_dir,
                )
            )
        elif args.command == "assemble-open-measurement-input":
            _json_output(
                assemble_open_measurement_input(
                    acquisition_receipt_path=args.acquisition_receipt,
                    shared_root=args.shared_root,
                    open_root=args.open_root,
                    pack_root=args.pack_root,
                    operator_input_dir=args.operator_input_dir,
                    measurement_root=args.measurement_root,
                    output_relative_path=args.output_relative_path,
                )
            )
        elif args.command == "admit-open-measurements":
            _json_output(
                admit_open_measurements(
                    acquisition_receipt_path=args.acquisition_receipt,
                    shared_root=args.shared_root,
                    open_root=args.open_root,
                    measurement_root=args.measurement_root,
                    admission_input_relative_path=args.admission_input,
                    output_dir=args.output_dir,
                )
            )
        elif args.command == "verify-open-measurement-admission":
            _json_output(
                verify_open_measurement_admission(
                    acquisition_receipt_path=args.acquisition_receipt,
                    admission_receipt_path=args.admission_receipt,
                    dataset_root=args.dataset_root,
                    shared_root=args.shared_root,
                    open_root=args.open_root,
                    measurement_root=args.measurement_root,
                )
            )
        elif args.command == "fit-open-selection-candidate":
            from .open_selection_fit_export import run_open_selection_fit_export

            _json_output(
                run_open_selection_fit_export(
                    acquisition_receipt_path=args.acquisition_receipt,
                    admission_receipt_path=args.admission_receipt,
                    dataset_root=args.dataset_root,
                    shared_root=args.shared_root,
                    open_root=args.open_root,
                    measurement_root=args.measurement_root,
                    output_dir=args.output_dir,
                )
            )
        elif args.command == "verify-open-selection-candidate-export":
            from .open_selection_fit_export import verify_open_selection_fit_export

            _json_output(
                verify_open_selection_fit_export(
                    acquisition_receipt_path=args.acquisition_receipt,
                    admission_receipt_path=args.admission_receipt,
                    dataset_root=args.dataset_root,
                    shared_root=args.shared_root,
                    open_root=args.open_root,
                    measurement_root=args.measurement_root,
                    export_root=args.export_root,
                )
            )
        elif args.command in {
            "solve-open-selection-recipe-candidate",
            "verify-open-selection-recipe-candidate",
        }:
            from .open_selection_recipe_solver import (
                solve_open_selection_recipe_candidate,
                verify_open_selection_recipe_candidate,
            )

            recipe_kwargs = {
                "acquisition_receipt_path": args.acquisition_receipt,
                "admission_receipt_path": args.admission_receipt,
                "dataset_root": args.dataset_root,
                "shared_root": args.shared_root,
                "open_root": args.open_root,
                "measurement_root": args.measurement_root,
                "fit_export_root": args.fit_export_root,
                "request_root": args.request_root,
                "request_relative_path": args.request_relative_path,
            }
            if args.command == "solve-open-selection-recipe-candidate":
                _json_output(
                    solve_open_selection_recipe_candidate(
                        output_dir=args.output_dir,
                        **recipe_kwargs,
                    )
                )
            else:
                _json_output(
                    verify_open_selection_recipe_candidate(
                        candidate_root=args.candidate_root,
                        **recipe_kwargs,
                    )
                )
        elif args.command in {
            "verify-holdout-preregistration",
            "run-independent-holdout-evaluation",
            "verify-independent-holdout-evaluation",
        }:
            from .independent_holdout_activation import (
                run_independent_holdout_evaluation,
                verify_holdout_preregistration,
                verify_independent_holdout_evaluation,
            )

            authority_kwargs = {
                "acquisition_receipt_path": args.acquisition_receipt,
                "admission_receipt_path": args.admission_receipt,
                "dataset_root": args.dataset_root,
                "shared_root": args.shared_root,
                "open_root": args.open_root,
                "measurement_root": args.measurement_root,
                "candidate_export_root": args.candidate_export_root,
                "baseline_model_path": args.baseline_model,
                "custody_commitment_path": args.custody_commitment,
                "repeatability_receipt_path": args.repeatability_receipt,
                "acceptance_profile_path": args.acceptance_profile,
                "colorimetry_profile_path": args.colorimetry_profile,
                "trust_store_path": args.trust_store,
                "preregistration_envelope_path": args.preregistration_envelope,
            }
            if args.command == "verify-holdout-preregistration":
                _json_output(verify_holdout_preregistration(**authority_kwargs))
            elif args.command == "run-independent-holdout-evaluation":
                _json_output(
                    run_independent_holdout_evaluation(
                        release_envelope_path=args.release_envelope,
                        sealed_input_path=args.sealed_input,
                        output_dir=args.output_dir,
                        release_ledger_dir=args.release_ledger_dir,
                        **authority_kwargs,
                    )
                )
            else:
                _json_output(
                    verify_independent_holdout_evaluation(
                        release_envelope_path=args.release_envelope,
                        sealed_input_path=args.sealed_input,
                        evaluation_root=args.evaluation_root,
                        release_ledger_dir=args.release_ledger_dir,
                        **authority_kwargs,
                    )
                )
        else:  # pragma: no cover - argparse enforces known commands.
            raise AssertionError(f"Unhandled command {args.command}")
    except (CalibrationError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    return 0
