import io
import os
import tempfile

# Importing receipt_scanner.app opens RECEIPT_DATA_DIR and loads .env, so point it somewhere harmless first.
# load_dotenv never overrides variables that are already set.
os.environ["RECEIPT_DATA_DIR"] = tempfile.mkdtemp(prefix="receipt-tests-")
os.environ["RECEIPT_UNWARPER"] = "none"
os.environ["ANTHROPIC_API_KEY"] = ""

from PIL import Image


def make_jpeg(width: int = 300, height: int = 500, shade: int = 200) -> bytes:
    buf = io.BytesIO()
    Image.new("L", (width, height), shade).save(buf, "JPEG")
    return buf.getvalue()
