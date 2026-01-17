import os
import asyncio
import json
import traceback
from datetime import datetime
from quart import Quart, render_template, request, jsonify, session
import google.generativeai as genai
import firebase_admin
from firebase_admin import credentials, auth, firestore
from functools import wraps

app = Quart(__name__)
app.secret_key = os.environ.get("APP_SECRET_KEY", "your-secret-key-change-in-production")

# ========== FIREBASE INITIALIZATION ==========
try:
    # For Render deployment, use environment variable for Firebase credentials
    firebase_config = os.environ.get("FIREBASE_CONFIG")
    
    if firebase_config:
        # Parse JSON from environment variable
        cred_dict = json.loads(firebase_config)
        cred = credentials.Certificate(cred_dict)
    else:
        # For local development, use service account file
        cred = credentials.Certificate("firebase-service-account.json")
    
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    firebase_initialized = True
    print("✅ Firebase initialized successfully")
except Exception as e:
    print(f"⚠️ Firebase initialization failed: {e}")
    firebase_initialized = False
    db = None

# ========== AUTHENTICATION DECORATORS ==========
def login_required(f):
    @wraps(f)
    async def decorated_function(*args, **kwargs):
        auth_header = request.headers.get('Authorization')
        
        if auth_header and auth_header.startswith('Bearer '):
            token = auth_header.split('Bearer ')[1]
            try:
                # Verify Firebase ID token
                decoded_token = auth.verify_id_token(token)
                request.user = decoded_token
                return await f(*args, **kwargs)
            except Exception as e:
                return jsonify({"error": "Invalid authentication token", "details": str(e)}), 401
        else:
            return jsonify({"error": "Authentication required"}), 401
    return decorated_function

def optional_auth(f):
    @wraps(f)
    async def decorated_function(*args, **kwargs):
        auth_header = request.headers.get('Authorization')
        
        if auth_header and auth_header.startswith('Bearer '):
            token = auth_header.split('Bearer ')[1]
            try:
                decoded_token = auth.verify_id_token(token)
                request.user = decoded_token
            except:
                request.user = None
        else:
            request.user = None
        
        return await f(*args, **kwargs)
    return decorated_function

# ========== FIREBASE DATABASE FUNCTIONS ==========
async def save_prompt_to_firestore(user_id, original_prompt, enhanced_prompt, prompt_type, metadata):
    """Save generated prompt to Firestore"""
    if not firebase_initialized or not db:
        return None
    
    try:
        prompt_data = {
            'user_id': user_id,
            'original_prompt': original_prompt,
            'enhanced_prompt': enhanced_prompt,
            'prompt_type': prompt_type,
            'metadata': metadata,
            'created_at': datetime.now(),
            'updated_at': datetime.now(),
            'word_count': len(enhanced_prompt.split()),
            'character_count': len(enhanced_prompt)
        }
        
        doc_ref = db.collection('prompts').document()
        await asyncio.get_event_loop().run_in_executor(
            None, 
            lambda: doc_ref.set(prompt_data)
        )
        
        return doc_ref.id
    except Exception as e:
        print(f"Error saving to Firestore: {e}")
        return None

async def get_user_prompts(user_id, limit=50):
    """Get user's prompt history"""
    if not firebase_initialized or not db:
        return []
    
    try:
        prompts_ref = db.collection('prompts')
        query = prompts_ref.where('user_id', '==', user_id)\
                          .order_by('created_at', direction=firestore.Query.DESCENDING)\
                          .limit(limit)
        
        docs = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: list(query.stream())
        )
        
        prompts = []
        for doc in docs:
            data = doc.to_dict()
            data['id'] = doc.id
            # Convert Firestore timestamp to ISO format
            if 'created_at' in data:
                data['created_at'] = data['created_at'].isoformat()
            prompts.append(data)
        
        return prompts
    except Exception as e:
        print(f"Error fetching prompts: {e}")
        return []

async def update_prompt_usage_stats(user_id):
    """Update user's prompt usage statistics"""
    if not firebase_initialized or not db:
        return
    
    try:
        user_ref = db.collection('users').document(user_id)
        
        # Get current stats or initialize
        doc = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: user_ref.get()
        )
        
        current_data = doc.to_dict() if doc.exists else {
            'total_prompts': 0,
            'prompts_today': 0,
            'last_reset_date': datetime.now().date().isoformat(),
            'created_at': datetime.now()
        }
        
        # Reset daily count if new day
        today = datetime.now().date().isoformat()
        if current_data.get('last_reset_date') != today:
            current_data['prompts_today'] = 0
            current_data['last_reset_date'] = today
        
        # Update counts
        current_data['total_prompts'] = current_data.get('total_prompts', 0) + 1
        current_data['prompts_today'] = current_data.get('prompts_today', 0) + 1
        current_data['updated_at'] = datetime.now()
        
        await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: user_ref.set(current_data, merge=True)
        )
    except Exception as e:
        print(f"Error updating stats: {e}")

# ========== AUTHENTICATION ROUTES ==========
@app.route('/auth/register', methods=['POST'])
async def register():
    """Register a new user with Firebase Authentication"""
    try:
        data = await request.json
        email = data.get('email')
        password = data.get('password')
        display_name = data.get('display_name')
        
        if not email or not password:
            return jsonify({"error": "Email and password are required"}), 400
        
        # Create user in Firebase Auth
        user = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: auth.create_user(
                email=email,
                password=password,
                display_name=display_name
            )
        )
        
        # Create user document in Firestore
        if firebase_initialized and db:
            user_data = {
                'uid': user.uid,
                'email': email,
                'display_name': display_name,
                'created_at': datetime.now(),
                'plan': 'free',  # Default plan
                'daily_limit': 100,
                'total_prompts': 0
            }
            
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: db.collection('users').document(user.uid).set(user_data)
            )
        
        # Generate custom token for client
        custom_token = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: auth.create_custom_token(user.uid)
        )
        
        return jsonify({
            "status": "success",
            "message": "User registered successfully",
            "user_id": user.uid,
            "custom_token": custom_token.decode('utf-8') if isinstance(custom_token, bytes) else custom_token,
            "email": user.email
        })
        
    except auth.EmailAlreadyExistsError:
        return jsonify({"error": "Email already exists"}), 400
    except Exception as e:
        return jsonify({"error": "Registration failed", "details": str(e)}), 500

@app.route('/auth/login', methods=['POST'])
async def login():
    """Login user and return Firebase ID token"""
    try:
        data = await request.json
        id_token = data.get('id_token')  # From Firebase Client SDK
        
        if not id_token:
            return jsonify({"error": "ID token is required"}), 400
        
        # Verify the ID token
        decoded_token = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: auth.verify_id_token(id_token)
        )
        
        uid = decoded_token['uid']
        
        # Get user data from Firestore
        user_data = None
        if firebase_initialized and db:
            user_doc = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: db.collection('users').document(uid).get()
            )
            if user_doc.exists:
                user_data = user_doc.to_dict()
        
        return jsonify({
            "status": "success",
            "user_id": uid,
            "user_data": user_data,
            "token_expiry": decoded_token.get('exp')
        })
        
    except Exception as e:
        return jsonify({"error": "Login failed", "details": str(e)}), 401

@app.route('/auth/user', methods=['GET'])
@login_required
async def get_current_user():
    """Get current user's information"""
    try:
        uid = request.user['uid']
        
        if firebase_initialized and db:
            user_doc = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: db.collection('users').document(uid).get()
            )
            
            if user_doc.exists:
                user_data = user_doc.to_dict()
                # Get user's prompt count
                prompts_query = db.collection('prompts')\
                                 .where('user_id', '==', uid)
                
                prompt_count = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: len(list(prompts_query.stream()))
                )
                
                user_data['prompt_count'] = prompt_count
                
                return jsonify({
                    "status": "success",
                    "user": user_data
                })
        
        return jsonify({
            "status": "success",
            "user": {"uid": uid}
        })
        
    except Exception as e:
        return jsonify({"error": "Failed to get user data", "details": str(e)}), 500

# ========== ENHANCED GENERATE ROUTE WITH AUTH ==========
@app.route('/generate', methods=['POST'])
@optional_auth  # Changed to optional_auth to allow both authenticated and guest usage
async def generate():
    data = await request.json
    user_input = data.get("prompt", "").strip()
    prompt_type = data.get("type", "text").lower()
    complexity = data.get("complexity", "detailed")
    
    if not user_input:
        return jsonify({"error": "Input is empty"}), 400
    
    # Check rate limiting for authenticated users
    if hasattr(request, 'user') and request.user:
        user_id = request.user['uid']
        
        # Get user's plan and limits
        if firebase_initialized and db:
            user_doc = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: db.collection('users').document(user_id).get()
            )
            
            if user_doc.exists:
                user_data = user_doc.to_dict()
                daily_limit = user_data.get('daily_limit', 100)
                prompts_today = user_data.get('prompts_today', 0)
                
                if prompts_today >= daily_limit:
                    return jsonify({
                        "error": "Daily limit reached",
                        "limit": daily_limit,
                        "used": prompts_today
                    }), 429
    
    # Build context-aware input
    enhancement_context = f"""
    ENHANCEMENT REQUEST:
    User Input: "{user_input}"
    Prompt Type: {prompt_type.upper()}
    Desired Complexity: {complexity.upper()}
    
    Please apply the appropriate enhancement framework for {prompt_type} prompts.
    """
    
    try:
        # Add model-specific instructions
        type_specific_instruction = ""
        if prompt_type == "image":
            type_specific_instruction = " Focus on visual details, composition, and artistic elements."
        elif prompt_type == "video":
            type_specific_instruction = " Emphasize movement, sequencing, and cinematic techniques."
        elif prompt_type == "code":
            type_specific_instruction = " Include specific requirements, edge cases, and testing scenarios."
        
        full_input = enhancement_context + type_specific_instruction + "\n\nEnhanced Professional Prompt:"
        
        # Generate enhanced prompt
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None, 
            lambda: model.generate_content(full_input)
        )
        
        enhanced_prompt = response.text.strip()
        quality_metrics = analyze_prompt_quality(enhanced_prompt, prompt_type)
        
        # Prepare metadata
        metadata = {
            "type": prompt_type,
            "complexity": complexity,
            "quality_score": quality_metrics["quality_score"],
            "model_used": "gemini-1.5-flash",
            "timestamp": datetime.now().isoformat()
        }
        
        # Save to Firestore if user is authenticated
        prompt_id = None
        if hasattr(request, 'user') and request.user:
            user_id = request.user['uid']
            
            # Save prompt
            prompt_id = await save_prompt_to_firestore(
                user_id, user_input, enhanced_prompt, prompt_type, metadata
            )
            
            # Update usage statistics
            await update_prompt_usage_stats(user_id)
        
        return jsonify({
            "status": "success",
            "professional_prompt": enhanced_prompt,
            "type": prompt_type,
            "complexity": complexity,
            "quality_metrics": quality_metrics,
            "word_count": len(enhanced_prompt.split()),
            "character_count": len(enhanced_prompt),
            "prompt_id": prompt_id,
            "authenticated": hasattr(request, 'user') and request.user is not None
        })
        
    except Exception as e:
        app.logger.error(f"Error generating prompt: {str(e)}")
        return jsonify({
            "error": str(e),
            "fallback_prompt": generate_fallback_prompt(user_input, prompt_type)
        }), 500

# ========== NEW ROUTES FOR PROMPT MANAGEMENT ==========
@app.route('/prompts/history', methods=['GET'])
@login_required
async def get_prompt_history():
    """Get user's prompt generation history"""
    try:
        user_id = request.user['uid']
        limit = request.args.get('limit', default=50, type=int)
        
        prompts = await get_user_prompts(user_id, limit)
        
        return jsonify({
            "status": "success",
            "count": len(prompts),
            "prompts": prompts
        })
        
    except Exception as e:
        return jsonify({"error": "Failed to get prompt history", "details": str(e)}), 500

@app.route('/prompts/<prompt_id>', methods=['GET'])
@login_required
async def get_prompt(prompt_id):
    """Get a specific prompt by ID"""
    try:
        if not firebase_initialized or not db:
            return jsonify({"error": "Database not available"}), 503
        
        doc_ref = db.collection('prompts').document(prompt_id)
        doc = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: doc_ref.get()
        )
        
        if not doc.exists:
            return jsonify({"error": "Prompt not found"}), 404
        
        data = doc.to_dict()
        
        # Check ownership
        if data.get('user_id') != request.user['uid']:
            return jsonify({"error": "Unauthorized access"}), 403
        
        data['id'] = doc.id
        
        # Convert timestamps
        if 'created_at' in data:
            data['created_at'] = data['created_at'].isoformat()
        
        return jsonify({
            "status": "success",
            "prompt": data
        })
        
    except Exception as e:
        return jsonify({"error": "Failed to get prompt", "details": str(e)}), 500

@app.route('/prompts/<prompt_id>', methods=['DELETE'])
@login_required
async def delete_prompt(prompt_id):
    """Delete a specific prompt"""
    try:
        if not firebase_initialized or not db:
            return jsonify({"error": "Database not available"}), 503
        
        doc_ref = db.collection('prompts').document(prompt_id)
        doc = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: doc_ref.get()
        )
        
        if not doc.exists:
            return jsonify({"error": "Prompt not found"}), 404
        
        data = doc.to_dict()
        
        # Check ownership
        if data.get('user_id') != request.user['uid']:
            return jsonify({"error": "Unauthorized access"}), 403
        
        await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: doc_ref.delete()
        )
        
        return jsonify({
            "status": "success",
            "message": "Prompt deleted successfully"
        })
        
    except Exception as e:
        return jsonify({"error": "Failed to delete prompt", "details": str(e)}), 500

@app.route('/user/stats', methods=['GET'])
@login_required
async def get_user_stats():
    """Get user's usage statistics"""
    try:
        user_id = request.user['uid']
        
        if not firebase_initialized or not db:
            return jsonify({"error": "Database not available"}), 503
        
        # Get user document
        user_doc = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: db.collection('users').document(user_id).get()
        )
        
        if not user_doc.exists:
            return jsonify({"error": "User not found"}), 404
        
        user_data = user_doc.to_dict()
        
        # Get prompt statistics
        prompts_ref = db.collection('prompts')
        
        # Total prompts
        total_query = prompts_ref.where('user_id', '==', user_id)
        total_count = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: len(list(total_query.stream()))
        )
        
        # Today's prompts
        today = datetime.now().date()
        today_start = datetime.combine(today, datetime.min.time())
        today_query = prompts_ref.where('user_id', '==', user_id)\
                                 .where('created_at', '>=', today_start)
        today_count = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: len(list(today_query.stream()))
        )
        
        # Prompts by type
        type_stats = {}
        for ptype in ['text', 'image', 'video', 'code', 'audio', 'data']:
            type_query = prompts_ref.where('user_id', '==', user_id)\
                                    .where('prompt_type', '==', ptype)
            type_count = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda tq=type_query: len(list(tq.stream()))
            )
            if type_count > 0:
                type_stats[ptype] = type_count
        
        return jsonify({
            "status": "success",
            "stats": {
                "total_prompts": total_count,
                "prompts_today": today_count,
                "daily_limit": user_data.get('daily_limit', 100),
                "plan": user_data.get('plan', 'free'),
                "prompts_by_type": type_stats,
                "account_created": user_data.get('created_at').isoformat() if user_data.get('created_at') else None
            }
        })
        
    except Exception as e:
        return jsonify({"error": "Failed to get stats", "details": str(e)}), 500

# ========== EXISTING ROUTES (UPDATED FOR AUTH) ==========
@app.route('/')
async def index():
    return await render_template('index.html')

@app.route('/templates', methods=['GET'])
@optional_auth
async def get_templates():
    return jsonify({
        "templates": PROMPT_TEMPLATES,
        "supported_types": ["text", "image", "video", "code", "audio", "data"],
        "complexity_levels": ["simple", "detailed", "comprehensive"],
        "authenticated": hasattr(request, 'user') and request.user is not None
    })

@app.route('/analyze', methods=['POST'])
@optional_auth
async def analyze_prompt():
    data = await request.json
    existing_prompt = data.get("prompt", "")
    
    if not existing_prompt:
        return jsonify({"error": "No prompt provided"}), 400
    
    analysis_request = f"""
    Analyze this existing prompt and suggest improvements:
    
    EXISTING PROMPT:
    {existing_prompt}
    
    Please provide:
    1. Strengths of the current prompt
    2. Areas for improvement
    3. Specific suggestions to enhance clarity, specificity, and effectiveness
    4. Alternative phrasing for key sections
    5. Missing elements that should be added
    
    Be constructive and specific in your feedback.
    """
    
    try:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: model.generate_content(analysis_request)
        )
        
        # Save analysis if user is authenticated
        analysis_id = None
        if hasattr(request, 'user') and request.user:
            user_id = request.user['uid']
            
            if firebase_initialized and db:
                analysis_data = {
                    'user_id': user_id,
                    'original_prompt': existing_prompt,
                    'analysis': response.text.strip(),
                    'type': 'analysis',
                    'created_at': datetime.now()
                }
                
                doc_ref = db.collection('analyses').document()
                await loop.run_in_executor(
                    None,
                    lambda: doc_ref.set(analysis_data)
                )
                analysis_id = doc_ref.id
        
        return jsonify({
            "status": "success",
            "analysis": response.text.strip(),
            "original_prompt": existing_prompt,
            "word_count": len(existing_prompt.split()),
            "analysis_id": analysis_id
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ========== HELPER FUNCTIONS (KEEP EXISTING) ==========
# [Keep all your existing helper functions: analyze_prompt_quality, 
# generate_recommendations, generate_fallback_prompt, etc.]

# [Your existing SYSTEM_INSTRUCTION, model initialization, 
# PROMPT_TEMPLATES, and other configurations remain the same]
@app.route('/templates', methods=['GET'])
async def get_templates():
    """Get available prompt templates"""
    return jsonify({
        "templates": PROMPT_TEMPLATES,
        "supported_types": ["text", "image", "video", "code", "audio", "data"],
        "complexity_levels": ["simple", "detailed", "comprehensive"]
    })

@app.route('/analyze', methods=['POST'])
async def analyze_prompt():
    """Analyze an existing prompt for improvements"""
    data = await request.json
    existing_prompt = data.get("prompt", "")
    
    if not existing_prompt:
        return jsonify({"error": "No prompt provided"}), 400
    
    analysis_request = f"""
    Analyze this existing prompt and suggest improvements:
    
    EXISTING PROMPT:
    {existing_prompt}
    
    Please provide:
    1. Strengths of the current prompt
    2. Areas for improvement
    3. Specific suggestions to enhance clarity, specificity, and effectiveness
    4. Alternative phrasing for key sections
    5. Missing elements that should be added
    
    Be constructive and specific in your feedback.
    """
    
    try:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: model.generate_content(analysis_request)
        )
        
        return jsonify({
            "status": "success",
            "analysis": response.text.strip(),
            "original_prompt": existing_prompt,
            "word_count": len(existing_prompt.split())
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def analyze_prompt_quality(prompt: str, prompt_type: str) -> dict:
    """Analyze the quality of an enhanced prompt"""
    words = prompt.split()
    word_count = len(words)
    
    quality_indicators = {
        "specificity": any(word in prompt.lower() for word in 
                          ["specific", "detailed", "exact", "precise", "concrete"]),
        "structure": any(word in prompt.lower() for word in 
                        ["format", "structure", "section", "outline", "template"]),
        "constraints": any(word in prompt.lower() for word in 
                          ["must", "should", "require", "constraint", "limit"]),
        "examples": "example" in prompt.lower() or "for instance" in prompt.lower(),
        "tone_appropriate": any(word in prompt.lower() for word in 
                               ["professional", "formal", "academic", "technical"]),
        "has_objective": word_count > 20 and any(word in prompt.lower() for word in 
                                                ["objective", "goal", "purpose", "aim"]),
        "appropriate_length": 50 <= word_count <= 500
    }
    
    # Type-specific checks
    if prompt_type == "image":
        quality_indicators["visual_elements"] = any(word in prompt.lower() for word in 
                                                   ["lighting", "composition", "style", "angle", "resolution"])
    elif prompt_type == "video":
        quality_indicators["temporal_elements"] = any(word in prompt.lower() for word in 
                                                     ["movement", "sequence", "timing", "duration", "pace"])
    elif prompt_type == "code":
        quality_indicators["technical_specs"] = any(word in prompt.lower() for word in 
                                                   ["function", "input", "output", "test", "error"])
    
    # Calculate quality score
    score = sum(quality_indicators.values())
    max_score = len(quality_indicators)
    quality_score = (score / max_score) * 100
    
    return {
        "quality_score": round(quality_score, 1),
        "indicators": quality_indicators,
        "word_count": word_count,
        "recommendations": generate_recommendations(quality_indicators, prompt_type)
    }

def generate_recommendations(indicators: dict, prompt_type: str) -> list:
    """Generate improvement recommendations based on quality indicators"""
    recommendations = []
    
    if not indicators["specificity"]:
        recommendations.append("Add more specific details and concrete requirements")
    
    if not indicators["structure"]:
        recommendations.append("Consider adding a clear structure or format specification")
    
    if not indicators["constraints"]:
        recommendations.append("Define constraints or limitations to guide the AI")
    
    if not indicators["examples"] and prompt_type in ["text", "code", "data"]:
        recommendations.append("Include examples to clarify expected output")
    
    if prompt_type == "image" and not indicators["visual_elements"]:
        recommendations.append("Add more visual details like lighting, composition, or style")
    
    if prompt_type == "video" and not indicators["temporal_elements"]:
        recommendations.append("Include temporal elements like pacing, sequencing, or duration")
    
    return recommendations

def generate_fallback_prompt(user_input: str, prompt_type: str) -> str:
    """Generate a fallback prompt when AI fails"""
    base_prompt = f"Professional {prompt_type.upper()} Prompt for: {user_input}\n\n"
    
    templates = {
        "text": "OBJECTIVE: Clearly define the primary goal\nCONTEXT: Provide necessary background\nFORMAT: Specify output structure\nKEY REQUIREMENTS: List essential elements\nTONE: Define appropriate tone\nCONSTRAINTS: Set clear limitations\nEXPECTED OUTPUT: Describe what success looks like",
        "image": "SUBJECT: Main focus of the image\nACTION: What's happening\nENVIRONMENT: Where it takes place\nSTYLE: Artistic approach\nLIGHTING: Lighting conditions\nCOMPOSITION: Framing and angle\nMOOD: Emotional atmosphere\nTECHNICAL: Resolution and quality",
        "video": "SCENE: Main visual sequence\nMOVEMENT: Camera and subject motion\nSTYLE: Cinematic approach\nPACING: Timing and rhythm\nAUDIO: Sound elements\nLENGTH: Duration\nASPECT RATIO: Frame dimensions",
        "code": "FUNCTION: What the code should do\nINPUT: Required inputs\nOUTPUT: Expected outputs\nCONSTRAINTS: Technical limitations\nERROR HANDLING: How to handle issues\nTESTING: Verification requirements\nDOCUMENTATION: Code documentation needs"
    }
    
    return base_prompt + templates.get(prompt_type, templates["text"])

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
