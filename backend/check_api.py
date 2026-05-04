import inspect
import azure.ai.documentintelligence as di
from azure.ai.documentintelligence.models import AnalyzeDocumentRequest

print("SDK version:", getattr(di, "__version__", "unknown"))
print()

# Check the method signature
client_cls = di.DocumentIntelligenceClient
sig = inspect.signature(client_cls.begin_analyze_document)
print("begin_analyze_document signature:", sig)
print()

# Check AnalyzeDocumentRequest signature
req_sig = inspect.signature(AnalyzeDocumentRequest.__init__)
print("AnalyzeDocumentRequest.__init__ signature:", req_sig)
