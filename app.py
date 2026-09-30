import os
import json
import uuid
import shutil
import urllib.parse
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any

from fastapi import FastAPI, HTTPException, Depends, File, UploadFile, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, EmailStr
from passlib.context import CryptContext
from jose import JWTError, jwt
from PIL import Image
from dotenv import load_dotenv

# Initialize Environment & App
load_dotenv()
app = FastAPI(title="PocketSmart: AI Budget Planner")

SECRET_KEY = os.getenv("SECRET_KEY", "pocketsmart_super_secret_key_2026")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

templates = Jinja2Templates(directory="templates")
if os.path.exists("static/uploads") and not os.path.isdir("static/uploads"):
    os.remove("static/uploads")
os.makedirs("static/uploads", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token", auto_error=False)

# Gemini AI Client Initialization (using google-genai and gemini-2.5-flash)
from google import genai
from google.genai import types

API_KEY = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
if not API_KEY:
    raise ValueError("Google API key not found in environment variables.")

ai_client = genai.Client(api_key=API_KEY)
AI_MODEL = "gemini-3.8-flash"

# In-Memory Databases & State Stores
users_db = {}
active_sessions = {}
user_recommendations = {}
blacklisted_tokens = set()

# Models & Schemas
class RegisterUser(BaseModel):
    username: str
    email: EmailStr
    full_name: Optional[str] = None
    password: str

class UserInDB(BaseModel):
    username: str
    email: str
    full_name: Optional[str] = None
    hashed_password: str

class Token(BaseModel):
    access_token: str
    token_type: str

class UserSession(BaseModel):
    username: str
    login_time: datetime
    last_activity: datetime
    token: str
    user_data: Dict[str, Any] = {}

class HistoryItem(BaseModel):
    id: str
    timestamp: str
    recommendation_type: str
    input_summary: Dict[str, Any]
    result_summary: Dict[str, Any]
    full_result: Dict[str, Any]

class HomeBudgetInput(BaseModel):
    total_budget: float
    num_lights: int = 4
    num_fans: int = 2
    num_furniture: int = 2
    num_dining_tables: int = 1
    has_living_room: bool = True
    has_kitchen: bool = True
    has_bedroom: bool = True
    additional_requirements: Optional[str] = None

class PartyBudgetInput(BaseModel):
    total_budget: float
    num_guests: int = 50
    party_type: str = "Birthday"
    venue_type: str = "Banquet Hall"
    needs_catering: bool = True
    needs_decoration: bool = True
    needs_entertainment: bool = True
    additional_requirements: Optional[str] = None

class JewelryBudgetInput(BaseModel):
    total_budget: float
    occasion: str = "Wedding"
    preferences: Optional[str] = None

# Helper Functions
def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=15))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

async def get_token(request: Request) -> Optional[str]:
    token = request.cookies.get("access_token")
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1] if " " in auth_header else None
    return token

async def get_current_user(request: Request, token: Optional[str] = Depends(get_token)) -> UserInDB:
    if not token or token in blacklisted_tokens:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        if not isinstance(username, str) or username not in users_db:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication credentials")
        return users_db[username]
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Could not validate credentials")

async def get_current_active_user(request: Request, current_user: UserInDB = Depends(get_current_user)) -> UserInDB:
    return current_user

def save_upload_file(upload_file: UploadFile) -> str:
    filename = upload_file.filename or "upload"
    ext = filename.rsplit(".", 1)[-1] if "." in filename else "jpg"
    saved_name = f"{uuid.uuid4()}.{ext}"
    filepath = os.path.join("static/uploads", saved_name)
    with open(filepath, "wb") as buffer:
        shutil.copyfileobj(upload_file.file, buffer)
    return filepath

def extract_json_from_response(text: Optional[str]) -> dict:
    response_text = text or ""
    try:
        clean_text = response_text.strip()
        if clean_text.startswith("```json"):
            clean_text = clean_text[7:]
        if clean_text.endswith("```"):
            clean_text = clean_text[:-3]
        return json.loads(clean_text.strip())
    except Exception:
        # Fallback dictionary if JSON parsing fails
        return {
            "total_budget": 5000.0,
            "budget_breakdown": [
                {
                    "category": "General",
                    "allocation": 5000.0,
                    "items": [
                        {
                            "name": "Standard Option",
                            "description": "AI recommendation generated successfully.",
                            "estimated_price": 5000.0,
                            "quantity": 1,
                            "search_terms": "home decor items"
                        }
                    ]
                }
            ],
            "additional_suggestions": ["Explore local marketplace discounts.", "Review bundle offers."]
        }

def save_to_history(username: str, recommendation_type: str, input_data: dict, result: dict):
    if username not in user_recommendations:
        user_recommendations[username] = []
    
    item_id = str(uuid.uuid4())[:8]
    timestamp = datetime.now().strftime("%B %d, %Y %I:%M %p")
    
    # Create summarized view
    summary = {
        "total_budget": result.get("total_budget", input_data.get("total_budget", 0)),
        "remaining_budget": result.get("remaining_budget", 0),
        "details": list(input_data.values())[:3]
    }
    
    hist_item = HistoryItem(
        id=item_id,
        timestamp=timestamp,
        recommendation_type=recommendation_type,
        input_summary=input_data,
        result_summary=summary,
        full_result=result
    )
    user_recommendations[username].append(hist_item)

# AI Recommendation Logic
def get_home_recommendations(budget_input: HomeBudgetInput) -> dict:
    prompt = f"""
    You are an expert interior design planner for India. Generate interior product recommendations for a home with a total budget of INR {budget_input.total_budget:.2f}.
    Requirements:
    - {budget_input.num_lights} lights/lighting fixtures
    - {budget_input.num_fans} ceiling fans
    - {budget_input.num_furniture} furniture pieces
    - {budget_input.num_dining_tables} dining tables
    Rooms: Living Room ({budget_input.has_living_room}), Kitchen ({budget_input.has_kitchen}), Bedroom ({budget_input.has_bedroom}).
    Additional: {budget_input.additional_requirements or 'None'}
    
    Return ONLY valid JSON matching this schema:
    {{
      "total_budget": {budget_input.total_budget},
      "remaining_budget": 500.0,
      "budget_breakdown": [
        {{
          "category": "lighting",
          "allocation": 1500.0,
          "items": [
            {{
              "name": "LED Warm Bulb",
              "description": "Energy efficient lighting.",
              "estimated_price": 300.0,
              "quantity": 5,
              "search_terms": "LED warm bulb"
            }}
          ]
        }}
      ],
      "calculation_table": [
        {{
          "category": "lighting",
          "items_count": 5,
          "total_cost": 1500.0,
          "percentage_of_budget": 30.0
        }}
      ],
      "additional_suggestions": ["Consider energy rating when purchasing appliances."]
    }}
    """
    response = ai_client.models.generate_content(model=AI_MODEL, contents=prompt)
    result = extract_json_from_response(response.text)
    
    # Attach shopping links
    for category in result.get("budget_breakdown", []):
        for item in category.get("items", []):
            st = item.get("search_terms", "")
            q = urllib.parse.quote_plus(st)
            item["shopping_links"] = {
                "amazon": f"https://www.amazon.in/s?k={q}",
                "flipkart": f"https://www.flipkart.com/search?q={q}",
                "ikea": f"https://www.ikea.com/in/en/search/?q={q}",
                "myntra": f"https://www.myntra.com/search?q={q}",
                "ajio": f"https://www.ajio.com/search/?text={q}"
            }
    return result

def get_party_recommendations(budget_input: PartyBudgetInput) -> dict:
    prompt = f"""
    You are an expert event planner in India. Plan a party with a total budget of INR {budget_input.total_budget:.2f}.
    Type: {budget_input.party_type}, Guests: {budget_input.num_guests}, Venue: {budget_input.venue_type}.
    Catering: {budget_input.needs_catering}, Decoration: {budget_input.needs_decoration}, Entertainment: {budget_input.needs_entertainment}.
    Additional: {budget_input.additional_requirements or 'None'}
    
    Return ONLY valid JSON matching this structure:
    {{
      "total_budget": {budget_input.total_budget},
      "budget_breakdown": [
        {{
          "category": "catering",
          "allocation": 2000.0,
          "items": [
            {{
              "name": "Catering Buffet",
              "description": "Standard veg buffet.",
              "estimated_price": 2000.0,
              "quantity": 1,
              "search_terms": "party catering service"
            }}
          ]
        }}
      ],
      "venue_suggestions": [
        {{
          "name": "Local Banquet",
          "type": "Hall",
          "capacity": {budget_input.num_guests},
          "estimated_cost": 1000.0,
          "search_terms": "banquet hall near me"
        }}
      ],
      "calculation_table_inr": [
        {{
          "category": "catering",
          "items_count": 1,
          "total_cost": 2000.0,
          "percentage_of_budget": 40.0
        }}
      ],
      "additional_suggestions": ["Book venues in advance for better rates."]
    }}
    """
    response = ai_client.models.generate_content(model=AI_MODEL, contents=prompt)
    result = extract_json_from_response(response.text)
    
    for category in result.get("budget_breakdown", []):
        for item in category.get("items", []):
            st = item.get("search_terms", "")
            q = urllib.parse.quote_plus(st)
            item["shopping_links"] = {
                "amazon": f"https://www.amazon.in/s?k={q}",
                "flipkart": f"https://www.flipkart.com/search?q={q}",
                "swiggy": f"https://www.swiggy.com/search?query={q}",
                "zomato": f"https://www.zomato.com/search?q={q}"
            }
            
    for venue in result.get("venue_suggestions", []):
        st = venue.get("search_terms", "")
        q = urllib.parse.quote_plus(st)
        venue["search_links"] = {
            "google": f"https://www.google.com/search?q={q}",
            "booking": f"https://www.booking.com/search.html?ss={q}",
            "oyorooms": f"https://www.oyorooms.com/search/?location={q}"
        }
    return result

def get_jewelry_recommendations(budget_input: JewelryBudgetInput, image_path: Optional[str] = None) -> dict:
    base_prompt = f"""
    You are an expert jewelry stylist in India. Recommend jewelry matching an outfit and occasion with a total budget of INR {budget_input.total_budget:.2f}.
    Occasion: {budget_input.occasion}, Preferences: {budget_input.preferences or 'None'}.
    
    Return ONLY valid JSON:
    {{
      "total_budget": {budget_input.total_budget},
      "remaining_budget": 500.0,
      "outfit_analysis": {{
        "colors": ["gold", "red"],
        "style": "ethnic",
        "formality": "formal"
      }},
      "jewelry_recommendations": [
        {{
          "item_type": "Necklace",
          "description": "Elegant gold-plated choker set.",
          "style": "Traditional",
          "estimated_price": 3000.0,
          "search_terms": "gold plated choker necklace"
        }}
      ],
      "styling_tips": ["Keep accessories balanced with outfit embroidery."]
    }}
    """
    
    if image_path and os.path.exists(image_path):
        pil_img = Image.open(image_path)
        response = ai_client.models.generate_content(
            model=AI_MODEL,
            contents=[base_prompt, pil_img]
        )
    else:
        response = ai_client.models.generate_content(model=AI_MODEL, contents=base_prompt)
        
    result = extract_json_from_response(response.text)
    
    for item in result.get("jewelry_recommendations", []):
        st = item.get("search_terms", "")
        q = urllib.parse.quote_plus(st)
        item["shopping_links"] = {
            "amazon": f"https://www.amazon.in/s?k={q}",
            "flipkart": f"https://www.flipkart.com/search?q={q}",
            "tanishq": f"https://www.tanishq.co.in/search?q={q}",
            "caratlane": f"https://www.caratlane.com/search?q={q}",
            "meesho": f"https://www.meesho.com/search?q={q}"
        }
    return result

# Web & API Routes
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"request": request})

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    token = await get_token(request)
    if token:
        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
            if payload.get("sub") in users_db:
                return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
        except Exception:
            pass
    return templates.TemplateResponse(request, "login.html", {"request": request})

@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse(request, "register.html", {"request": request})

@app.post("/register")
async def register_user(username: str = Form(...), email: str = Form(...), password: str = Form(...), full_name: Optional[str] = Form(None)):
    if username in users_db:
        raise HTTPException(status_code=400, detail="Username already registered")
    hashed_password = get_password_hash(password)
    users_db[username] = UserInDB(username=username, email=email, full_name=full_name, hashed_password=hashed_password)
    return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

@app.post("/token", response_model=Token)
async def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends()):
    user = users_db.get(form_data.username)
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Incorrect username or password")
    
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(data={"sub": user.username}, expires_delta=access_token_expires)
    
    active_sessions[user.username] = UserSession(
        username=user.username,
        login_time=datetime.utcnow(),
        last_activity=datetime.utcnow(),
        token=access_token
    )
    
    response = JSONResponse(content={"access_token": access_token, "token_type": "bearer"})
    response.set_cookie(key="access_token", value=access_token, httponly=True, max_age=3600, samesite="lax")
    return response

@app.post("/logout")
async def logout(request: Request):
    token = await get_token(request)
    if token:
        blacklisted_tokens.add(token)
    response = RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    response.delete_cookie(key="access_token")
    return response

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, current_user: UserInDB = Depends(get_current_active_user)):
    recent = user_recommendations.get(current_user.username, [])[-3:]
    return templates.TemplateResponse(request, "dashboard.html", {"request": request, "user": current_user, "recent": recent})

@app.get("/home-planner", response_class=HTMLResponse)
async def home_planner(request: Request, current_user: UserInDB = Depends(get_current_active_user)):
    return templates.TemplateResponse(request, "home_planner.html", {"request": request, "user": current_user})

@app.post("/home-budget")
async def plan_home_budget(
    total_budget: float = Form(...),
    num_lights: int = Form(4),
    num_fans: int = Form(2),
    num_furniture: int = Form(2),
    num_dining_tables: int = Form(1),
    has_living_room: bool = Form(True),
    has_kitchen: bool = Form(True),
    has_bedroom: bool = Form(True),
    additional_requirements: Optional[str] = Form(None),
    current_user: UserInDB = Depends(get_current_active_user)
):
    inp = HomeBudgetInput(
        total_budget=total_budget,
        num_lights=num_lights,
        num_fans=num_fans,
        num_furniture=num_furniture,
        num_dining_tables=num_dining_tables,
        has_living_room=has_living_room,
        has_kitchen=has_kitchen,
        has_bedroom=has_bedroom,
        additional_requirements=additional_requirements
    )
    result = get_home_recommendations(inp)
    save_to_history(current_user.username, "home", inp.dict(), result)
    return result

@app.get("/party-planner", response_class=HTMLResponse)
async def party_planner(request: Request, current_user: UserInDB = Depends(get_current_active_user)):
    return templates.TemplateResponse(request, "party_planner.html", {"request": request, "user": current_user})

@app.post("/party-budget")
async def plan_party_budget(
    total_budget: float = Form(...),
    num_guests: int = Form(50),
    party_type: str = Form("Birthday"),
    venue_type: str = Form("Banquet Hall"),
    needs_catering: bool = Form(True),
    needs_decoration: bool = Form(True),
    needs_entertainment: bool = Form(True),
    additional_requirements: Optional[str] = Form(None),
    current_user: UserInDB = Depends(get_current_active_user)
):
    inp = PartyBudgetInput(
        total_budget=total_budget,
        num_guests=num_guests,
        party_type=party_type,
        venue_type=venue_type,
        needs_catering=needs_catering,
        needs_decoration=needs_decoration,
        needs_entertainment=needs_entertainment,
        additional_requirements=additional_requirements
    )
    result = get_party_recommendations(inp)
    save_to_history(current_user.username, "party", inp.dict(), result)
    return result

@app.get("/jewelry-planner", response_class=HTMLResponse)
async def jewelry_planner(request: Request, current_user: UserInDB = Depends(get_current_active_user)):
    return templates.TemplateResponse(request, "jewelry_planner.html", {"request": request, "user": current_user})

@app.post("/jewelry-budget")
async def plan_jewelry_budget(
    total_budget: float = Form(...),
    occasion: str = Form("Wedding"),
    preferences: Optional[str] = Form(None),
    image: Optional[UploadFile] = File(None),
    current_user: UserInDB = Depends(get_current_active_user)
):
    inp = JewelryBudgetInput(
        total_budget=total_budget,
        occasion=occasion,
        preferences=preferences
    )
    image_path = save_upload_file(image) if image and image.filename else None
    result = get_jewelry_recommendations(inp, image_path)
    
    input_data = inp.dict()
    if image and image.filename:
        input_data["image"] = image.filename
        
    save_to_history(current_user.username, "jewelry", input_data, result)
    return result

@app.get("/history", response_class=HTMLResponse)
async def history_page(request: Request, current_user: UserInDB = Depends(get_current_active_user)):
    history_items = user_recommendations.get(current_user.username, [])
    return templates.TemplateResponse(request, "history.html", {"request": request, "user": current_user, "history": history_items})

@app.get("/recommendation-details/{rec_id}")
async def get_rec_details(rec_id: str, current_user: UserInDB = Depends(get_current_active_user)):
    items = user_recommendations.get(current_user.username, [])
    for item in items:
        if item.id == rec_id:
            return item.full_result
    raise HTTPException(status_code=404, detail="Recommendation not found")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)