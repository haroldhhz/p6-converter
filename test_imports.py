"""Quick import check for all required packages."""
packages = [
    "fastapi", "uvicorn", "pandas", "openpyxl",
    "python_dotenv", "openai",
    "azure.ai.documentintelligence",
    "azure.identity",
]
for pkg in packages:
    try:
        __import__(pkg)
        print(f"  OK  {pkg}")
    except ImportError as e:
        print(f"  FAIL {pkg}: {e}")
print("\nAll checks done.")
