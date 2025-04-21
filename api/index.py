from flask import Flask, request, jsonify, send_from_directory, render_template
from flask_bcrypt import Bcrypt
from flask_jwt_extended import JWTManager, create_access_token, jwt_required, get_jwt_identity
from flask_cors import CORS
from pymongo import MongoClient
from bson import ObjectId
import os
import uuid
import google.generativeai as genai
import smtplib
from email.mime.text import MIMEText
from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__, static_folder="static", template_folder="templates")
CORS(app)

# Configuration (make sure these ENV vars are set on Vercel Dashboard too)
app.config['JWT_SECRET_KEY'] = os.getenv('JWT_SECRET_KEY', 'fallback_secret_key')

# Updated UPLOAD_FOLDER path for Vercel environment (use /tmp)
UPLOAD_FOLDER = '/tmp/uploads'  # Using /tmp for Vercel compatibility
os.makedirs(UPLOAD_FOLDER, exist_ok=True)  # Ensure the folder exists inside /tmp
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

bcrypt = Bcrypt(app)
jwt = JWTManager(app)

# Gemini
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
gemini_model = genai.GenerativeModel('models/gemini-1.5-flash-002')

# MongoDB - change to Vercel-compatible Atlas URL later
client = MongoClient(os.getenv("MONGODB_URI", "mongodb://localhost:27017/"))
db = client.travel_reviews
users_collection = db.users
reviews_collection = db.reviews
ratings_collection = db.ratings

# USER AUTHENTICATION
@app.route('/signup', methods=['POST'])
def signup():
    data = request.get_json()
    name, email, password = data.get("name"), data.get("email"), data.get("password")
    address = data.get("address")
    phone = data.get("phone")
    if not name or not email or not password:
        return jsonify({"error": "Missing required fields"}), 400

    if users_collection.find_one({"email": email}):
        return jsonify({"error": "Email already registered"}), 409

    hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')
    users_collection.insert_one({
        "name": name,
        "email": email,
        "password": hashed_password,
        "address": address,
        "phone": phone
    })

    access_token = create_access_token(identity=email)

    return jsonify({
        "message": "Signup successful!",
        "token": access_token,
        "name": name
    }), 201

@app.route('/login', methods=['POST'])
def login():
    data = request.json
    email, password = data.get("email"), data.get("password")
    if not email or not password:
        return jsonify({"error": "Missing email or password"}), 400
    user = users_collection.find_one({"email": email})
    if user and bcrypt.check_password_hash(user["password"], password):
        access_token = create_access_token(identity=user["email"])
        return jsonify({"token": access_token, "message": "Login successful!", "name": user["name"]}), 200
    return jsonify({"error": "Invalid credentials"}), 401

@app.route('/protected', methods=['GET'])
@jwt_required()
def protected():
    return jsonify({"message": f"Hello, {get_jwt_identity()}"}), 200

# PHOTO UPLOADS
@app.route("/upload_photos", methods=["POST"])
def upload_photos():
    if "photos" not in request.files:
        return jsonify({"error": "No photos uploaded"}), 400
    files = request.files.getlist("photos")
    urls = []
    for file in files:
        filename = str(uuid.uuid4()) + os.path.splitext(file.filename)[1]
        path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        file.save(path)
        urls.append(f"/uploads/{filename}")
    return jsonify({"message": "Photos uploaded successfully!", "urls": urls}), 201

@app.route("/uploads/<filename>")
def get_uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

# ADD REVIEW
@app.route("/add_review", methods=["POST"])
def add_review():
    data = request.json
    review = {
        "Name": data["Name"],
        "location": data["location"],
        "purpose": data["purpose"],
        "budget": data["budget"],
        "transport": data["transport"],
        "review": data["review"],
        "images": data.get("photo_urls", []),
        "rating": 0,
        "rating_count": 0,
        "comments": []
    }
    reviews_collection.insert_one(review)
    return jsonify({"message": "Review added successfully!"}), 201

# GET REVIEWS WITH FILTERS
@app.route("/get_reviews", methods=["GET"])
def get_reviews():
    query = {}
    location = request.args.get("location")
    purpose = request.args.get("purpose")
    budget = request.args.get("budget")
    transport = request.args.get("transport")
    sort = request.args.get("sort")  # 'newest' or 'rating'

    if location:
        query["location"] = location
    if purpose:
        query["purpose"] = purpose
    if budget:
        query["budget"] = budget
    if transport:
        query["transport"] = transport

    sort_order = None
    if sort == "newest":
        sort_order = ("_id", -1)  # Sort by most recent
    elif sort == "rating":
        sort_order = ("rating", -1)  # Sort by highest rated

    reviews_cursor = reviews_collection.find(query)
    if sort_order:
        reviews_cursor = reviews_cursor.sort([sort_order])

    reviews = []
    for review in reviews_cursor:
        review["_id"] = str(review["_id"])  # Convert ObjectId to string
        reviews.append(review)

    return jsonify(reviews), 200

# ADD COMMENT
@app.route("/add_comment", methods=["POST"])
@jwt_required()
def add_comment():
    data = request.json
    review_id, comment = data.get("reviewId"), data.get("comment")
    if not review_id or not comment:
        return jsonify({"error": "Review ID and comment are required."}), 400
    user_email = get_jwt_identity()
    reviews_collection.update_one({"_id": ObjectId(review_id)}, {"$push": {"comments": {"user_email": user_email, "comment": comment}}})
    return jsonify({"message": "Comment added successfully!"}), 200

@app.route('/profile', methods=['GET'])
@jwt_required()
def profile():
    current_user = get_jwt_identity()  # Get the email of the currently authenticated user
    user = users_collection.find_one({"email": current_user})
    
    if user:
        response_data = {
            "name": user["name"],
            "email": user["email"],
            "address": user["address"],
            "phone": user["phone"]
        }
        return jsonify(response_data)
    else:
        return jsonify({"error": "User not found"}), 404

# UPDATE RATING
@app.route("/update_rating", methods=["POST"])
def update_rating():
    data = request.json
    review_id = data.get("reviewId")
    rating = data.get("rating")

    if not review_id or not rating:
        return jsonify({"error": "Review ID and rating are required."}), 400

    if not (1 <= rating <= 5):
        return jsonify({"error": "Rating must be between 1 and 5."}), 400

    # Get the review and update the rating
    review = reviews_collection.find_one({"_id": ObjectId(review_id)})
    if not review:
        return jsonify({"error": "Review not found."}), 404

    # Calculate new rating
    total_rating = review["rating"] * review["rating_count"] + rating
    new_rating_count = review["rating_count"] + 1
    new_avg_rating = total_rating / new_rating_count

    reviews_collection.update_one(
        {"_id": ObjectId(review_id)},
        {
            "$set": {
                "rating": new_avg_rating,
                "rating_count": new_rating_count
            }
        }
    )

    return jsonify({"message": "Rating updated successfully!"}), 200

@app.route("/generate_itinerary", methods=["POST"])
def generate_itinerary():
    data = request.json

    # Getting inputs from the user
    destination = data.get("destination")
    budget = data.get("budget")
    transport = data.get("transport")
    dates = data.get("dates", "not specified")
    purpose = data.get("purpose", "general travel")

    # Ensure all necessary data is provided
    if not all([destination, budget, transport]):
        return jsonify({"error": "Destination, budget, and transport are required."}), 400

    # Create a prompt based on the user inputs
    prompt = f"""
    Generate a personalized travel itinerary based on the following:
    - Destination: {destination}
    - Budget: ${budget}
    - Transport Preference: {transport}
    - Travel Dates: {dates}
    - Purpose: {purpose}

    The itinerary should include:
    - Suggested activities per day
    - Places to eat
    - Local transport advice
    - Accommodation options within budget
    - Estimated cost breakdown
    """

    try:
        # Call Gemini API to generate the itinerary
        response = gemini_model.generate_content(prompt)
        
        # Check if the response is valid
        if response.text:
            return jsonify({"itinerary": response.text})
        else:
            return jsonify({"error": "Failed to generate itinerary, no content returned."}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/send-message", methods=["POST"])
def send_message():
    try:
        name = request.form.get("name")
        email = request.form.get("email")
        message = request.form.get("message")

        if not name or not email or not message:
            raise ValueError("Name, email, and message are required.")

        msg_body = f"""
        📬 New message from Traveller's Verdict:

        👤 Name: {name}
        📧 Email: {email}
        📝 Message:
        {message}
        """

        # Update this with the sender and receiver email details
        sender_email = "travellers.verdict@gmail.com"  # Your Gmail
        receiver_email = "travellers.verdict@gmail.com"  # Where you want to receive the messages
        app_password = "your_app_password_here"  # Your Gmail app password

        msg = MIMEText(msg_body)
        msg["Subject"] = "New Contact Message"
        msg["From"] = sender_email
        msg["To"] = receiver_email

        # Send the email
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(sender_email, app_password)
            server.send_message(msg)

        return jsonify({"message": "Message sent successfully!"}), 200

    except Exception as e:
        return jsonify({"error": f"Internal server error: {str(e)}"}), 500
