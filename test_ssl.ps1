$env:SSL_CERT_FILE = "C:\Users\haroldhuang\Documents\cacert.pem"
$env:SSL_CERT_DIR = "C:\Users\haroldhuang\Documents\cacert.pem"
$py = "C:\Users\haroldhuang\OneDrive - Microsoft\Process and Standard\Demo AI Projects\p6-converter\venv\Scripts\python.exe"
& $py -c "import certifi; print('certifi.where():', certifi.where()); import ssl; print('SSL_DEFAULT_CERT:', ssl.get_default_verify_paths().cafile)"
