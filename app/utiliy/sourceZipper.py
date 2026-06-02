from pathlib import Path
import zipfile


version_number = "v1.0.0"

folders_to_zip = {
    "frontend_quant_app": r"C:\Tharun\Projects\SourceCode\frontend_quant\app",
    "quant_ui_react_src": r"C:\Tharun\Projects\SourceCode\frontend_quant_UI\Quant_UI_REACT\src",
    "backtest_engine_src": r"C:\Tharun\Projects\SourceCode\BacktestEngine\engine\src",
}

output_folder = Path(r"C:\Tharun\Projects\zipped_output")
output_folder.mkdir(parents=True, exist_ok=True)


def zip_folder(zip_name: str, folder_path: str, output_dir: Path, version: str):
    folder = Path(folder_path)

    if not folder.exists():
        print(f"Skipped, folder not found: {folder}")
        return

    if not folder.is_dir():
        print(f"Skipped, not a folder: {folder}")
        return

    zip_file_path = output_dir / f"{zip_name}_{version}.zip"

    with zipfile.ZipFile(zip_file_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for file_path in folder.rglob("*"):
            if file_path.is_file():
                arcname = file_path.relative_to(folder.parent)
                zipf.write(file_path, arcname)

    print(f"Created: {zip_file_path}")


for zip_name, folder_path in folders_to_zip.items():
    zip_folder(zip_name, folder_path, output_folder, version_number)

print("All folders zipped successfully.")