

import base64
import json
import os
import sqlite3
import subprocess
import tempfile
import time
from io import BytesIO
import google.generativeai as genai
import yt_dlp
from PIL import Image
from flask import Flask, request, jsonify, g, session
from flask_bcrypt import Bcrypt
from flask_cors import CORS
from dotenv import load_dotenv
from werkzeug.utils import secure_filename

load_dotenv()  # Load variables from .env

# Set up the environment and configure Generative AI client
api_key = os.getenv('API_KEY')
if not api_key:
    raise ValueError("API_KEY environment variable not set.")
genai.configure(api_key=api_key)

# Set up the Flask application
app = Flask(__name__, static_folder='src/frontend/build', static_url_path='/')
app.secret_key = os.getenv('SECRET_KEY', 'default-secret-key')  # Use a default key for local testing if necessary
CORS(app, origins=["http://localhost:3000"])  # Enable CORS for local frontend
bcrypt = Bcrypt(app)  # Set up Bcrypt for password hashing
app.config['DATABASE'] = 'database.db'  # Database file name
model = genai.GenerativeModel("gemini-1.5-flash")  # Set up generative AI model

# Function to get a database connection
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(app.config['DATABASE'])
        g.db.row_factory = sqlite3.Row
    return g.db

# Close the database connection after the app context ends
@app.teardown_appcontext
def close_db(error):
    db = g.pop('db', None)
    if db is not None:
        db.close()

# Initialize the database tables
def init_db():
    db = get_db()
    db.execute('''CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL UNIQUE,
                    password TEXT NOT NULL)''')
    db.commit()

    db.execute('''CREATE TABLE IF NOT EXISTS stories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    email TEXT NOT NULL,
                    story TEXT NOT NULL,
                    image BLOB,
                    mime_type TEXT,
                    approved BOOLEAN DEFAULT 0)''')
    db.commit()

    db.execute('''CREATE TABLE IF NOT EXISTS GuideEntry (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    topic TEXT NOT NULL,
                    day INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    approaches TEXT NOT NULL,
                    completed INTEGER DEFAULT 0,
                    FOREIGN KEY(user_id) REFERENCES users(id))''')
    db.commit()

    db.execute('''CREATE TABLE IF NOT EXISTS UserProgress (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    topic TEXT NOT NULL,
                    day INTEGER NOT NULL,
                    completed BOOLEAN DEFAULT 0,
                    UNIQUE(user_id, topic, day),
                    FOREIGN KEY(user_id) REFERENCES users(id))''')
    db.commit()

# Command to initialize the database from command line
@app.cli.command('init-db')
def init_db_command():
    init_db()
    print("Initialized the database.")

# Endpoint to add a user (for testing purposes)
@app.route('/add_user', methods=['POST'])
def add_user():
    db = get_db()
    db.execute('INSERT INTO users (email, password) VALUES (?, ?)',
               ('testuser@example.com', bcrypt.generate_password_hash('password').decode('utf-8')))
    db.commit()
    return 'User added!'

# API endpoint for retrieving a sample message
@app.route('/api/data', methods=['GET'])
def get_data():
    # Provide a simple message in JSON format
    data = {'message': 'Hello from Flask!'}
    return jsonify(data)
    
# Root endpoint for serving the index.html file
@app.route('/')
def index():
    # Log information about the static folder and file being served
    print(f"static_folder: {app.static_folder}")
    print(f"Trying to serve {os.path.join(app.static_folder, 'index.html')}")

    # Serve the index.html from the static folder
    return send_from_directory(app.static_folder, 'index.html')

# Signup endpoint for new users
@app.route('/signup', methods=['POST'])
def signup():
    # Get the email and password from the request
    data = request.json
    email = data.get('email')
    password = data.get('password')

    # Validate the input: both email and password must be provided
    if not email or not password:
        return jsonify({'error': 'Please provide an email and password'}), 400

    # Hash the password using Bcrypt
    hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')

    # Insert the new user into the database
    try:
        db = get_db()
        db.execute("INSERT INTO users (email, password) VALUES (?, ?)", (email, hashed_password))
        db.commit()
        return jsonify({"message": "User registered successfully"}), 201
    except sqlite3.IntegrityError as e:
        # Handle unique constraint error (email already exists)
        print(f"IntegrityError: {e}")
        return jsonify({"error": "User with this email already exists"}), 400
    except Exception as e:
        # Handle any unexpected error
        print(f"Unexpected error: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500

# Logout endpoint to clear the user session
@app.route('/logout')
def logout():
    session.clear()  # This clears the entire session
    return jsonify({"message": "Logged out successfully"}), 200


# Login endpoint for user authentication
@app.route('/login', methods=['POST'])
def login():
    # Get the email and password from the request
    data = request.json
    email = data.get('email')
    password = data.get('password')

    # Ensure that both email and password are provided
    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 400

    db = get_db()

    # Attempt to retrieve the user record by email
    try:
        user_record = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    except sqlite3.Error as e:
        # Log any database error and return an error response
        print("Database error:", e)
        return jsonify({"error": "Database error"}), 500

    # Check if the user record was found and verify the password
    if user_record is None:
        return jsonify({"error": "Invalid credentials"}), 401

    # Verify the password using Bcrypt
    if bcrypt.check_password_hash(user_record['password'], password):
        # Store the user ID in the session if login is successful
        session['user_id'] = user_record['id']
        return jsonify({"message": "Login successful"}), 200
    else:
        return jsonify({"error": "Invalid credentials"}), 401

# Function to get the current logged-in user from the session
def get_current_user():
    # Get the user_id from the session
    user_id = session.get('user_id')
    if user_id is None:
        return None

    # Fetch the user from the database using the user_id
    db = get_db()
    return db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

# List of topics for guides
topics = [
    "Building Your Club",
    "Getting Certifications and Courses",
    "Building Confidence",
    "Recognizing Healthy Relationships",
    "Saving Your First $1,000",
    "Improving Communication Skills"
]

# Function to generate a 21-day guide based on the selected goal
def generate_guide(goal_index, max_retries=5):
    """Generates a 21-day plan based on the selected goal."""
    
    # Validate the goal index
    if not (0 <= goal_index < len(topics)):
        raise ValueError("Invalid goal index.")
    
    # Prepare prompt to generate content using the selected goal
    goal = topics[goal_index]
    prompt = f'''You are a mentor dedicated to helping young adult girls develop new skills to enhance their career potential. 
                As an expert in goal-setting and habit-building, you specialize in creating 21-day plans that break down goals into manageable daily steps for building lasting habits.
                These habits are intended to support the achievement of a larger goal.
                While the goal might not be fully accomplished within 21 days, this period serves as a foundation to develop skills and continue working towards the ultimate objective.
                Keep in mind that these girls are between the ages of 14 and 19, each with unique circumstances, so craft responses that cater to these differences.
                For each day, offer more than one approach to help achieve their goal.
                Your task is to:
                1. Provide Daily Guidance for the goal: Deliver clear, relevant, and easy-to-understand advice to support the user in reaching each day’s objective.
                2. Break the Goal into 21 Steps: Divide the goal into 21 actionable daily tasks, each designed to build progressively toward accomplishing the larger goal.
                3. Output as raw JSON structure that follows the format below (do not include the json headline):
                {{
                "goal": "<the goal description>",
                "guide": [
                    {{
                    "day": 1,
                    "title": "<Day 1 activity title>",
                    "approaches": [
                        "<First approach to achieve the daily goal>",
                        "<Second approach to achieve the daily goal>",
                        ...
                    ]
                    }},
                    {{
                    "day": 2,
                    "title": "<Day 2 activity title>",
                    "approaches": [
                        "<First approach to achieve the daily goal>",
                        "<Second approach to achieve the daily goal>",
                        ...
                    ]
                    }},
                    ...
                    {{
                    "day": 21,
                    "title": "<Day 21 activity title>",
                    "approaches": [
                        "<First approach to achieve the daily goal>",
                        "<Second approach to achieve the daily goal>",
                        ...
                    ]
                    }}
                ]
                }}
                The goal for this user is {goal}'''

    response = model.generate_content(prompt)
    retries = 0
    
    while retries < max_retries:
        response = model.generate_content(prompt)
        
        try:
            # Attempt to decode the model's response
            guide_data = json.loads(response.text.strip())
            
            # Check if the response contains a 'guide' key
            if "guide" in guide_data:
                return guide_data  # Success: return the correctly formatted response

            print("Model response missing 'guide' key, retrying...")

        except json.JSONDecodeError:
            # Log JSON decode errors
            print(f"Failed to decode JSON on attempt {retries + 1}. Raw response: {response.text}")

        # Increment retry counter
        retries += 1

    # If no valid response after retries
    print("Exceeded maximum retries. Model response was not in the correct format.")
    return None

# Endpoint to get the guide for a specific topic
@app.route('/get-guide/<topic>', methods=['GET'])
def get_guide(topic):
    db = get_db()
    user = get_current_user()  # Get the current user
    user_id = user['id']

    # Fetch existing guide entries for the topic and user
    guide_entries = db.execute(
        'SELECT day, title, approaches, completed FROM GuideEntry WHERE topic = ? AND user_id = ? ORDER BY day', 
        (topic, user_id)
    ).fetchall()

    # If guide doesn't exist, generate a new guide
    if not guide_entries:
        goal_index = topics.index(topic) if topic in topics else -1
        if goal_index == -1:
            return jsonify({"error": "Topic not found"}), 404

        guide_data = generate_guide(goal_index)
        
        # Store the generated guide in the database
        for day_item in guide_data["guide"]:
            db.execute('''
                INSERT OR IGNORE INTO GuideEntry (user_id, topic, day, title, approaches, completed)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                user_id,
                topic,
                day_item["day"],
                day_item["title"],
                json.dumps(day_item["approaches"]),
                0  # Default to not completed
            ))
        db.commit()
        
        # Fetch the newly created guide entries
        guide_entries = db.execute(
            'SELECT day, title, approaches, completed FROM GuideEntry WHERE topic = ? AND user_id = ? ORDER BY day', 
            (topic, user_id)
        ).fetchall()

    # Format the guide data to return to the user
    guide = [
        {
            "day": entry["day"],
            "title": entry["title"],
            "approaches": json.loads(entry["approaches"]),
            "completed": bool(entry["completed"])
        }
        for entry in guide_entries
    ]

    return jsonify({"daily_guide": {"goal": topic, "guide": guide}})

# Endpoint to manually generate a guide plan for a specific topic (for testing purposes)
@app.route('/generate-plan', methods=['POST'])
def generate_plan():
    data = request.json
    goal_index = data.get('goal_index')

    # Validate the goal index
    if not (0 <= goal_index < len(topics)):
        return jsonify({"error": "Invalid goal index"}), 400

    # Generate the guide for the given goal index
    topic = topics[goal_index]
    guide_data = generate_guide(goal_index)

    if not guide_data:
        return jsonify({"error": "Failed to generate guide"}), 500

    # Store the generated guide in the database
    db = get_db()
    for day_item in guide_data["guide"]:
        db.execute('''
            INSERT OR IGNORE INTO GuideEntry (topic, day, title, approaches, completed)
            VALUES (?, ?, ?, ?, ?)
        ''', (
            topic,
            day_item["day"],
            day_item["title"],
            json.dumps(day_item["approaches"]),
            0  # Default to not completed
        ))
    db.commit()
    
    return jsonify({"daily_guide": guide_data}), 200

@app.route('/get-user-progress', methods=['POST'])
def get_user_progress():
    # Get the current logged-in user
    user = get_current_user()
    
    # Check if user is logged in
    if user is None:
        print("No user is logged in.")
        return jsonify({"error": "User not logged in"}), 401
    
    # Fetch user's progress
    print(f"Fetching progress for user_id: {user['id']}")
    db = get_db()
    progress = db.execute('''
        SELECT topic, day, completed
        FROM UserProgress
        WHERE user_id = ?
    ''', (user['id'],)).fetchall()

    # Log if no progress records are found
    if not progress:
        print(f"No progress records found for user_id: {user['id']}")
    
    # Format progress data as a dictionary for the frontend
    progress_dict = {}
    for row in progress:
        topic = row["topic"]
        if topic not in progress_dict:
            progress_dict[topic] = []
        progress_dict[topic].append({"day": row["day"], "completed": bool(row["completed"])})

    print("Progress data being sent:", progress_dict)
    return jsonify(progress=progress_dict)

@app.route('/update-day-completion', methods=['POST'])
def update_day_completion():
    # Retrieve data from the request
    data = request.json
    user = get_current_user()
    
    # Check if user is logged in
    if user is None:
        return jsonify({"error": "User not logged in"}), 401

    user_id = user['id']
    topic = data.get("topic")
    day = data.get("day")
    completed = data.get("completed", True)

    # Validate required data
    if user_id is None or topic is None or day is None:
        return jsonify({"error": "Missing required data"}), 400

    # Update or insert user's progress for the given topic and day
    db = get_db()
    try:
        # Insert or update the entry in UserProgress using ON CONFLICT
        db.execute('''
            INSERT INTO UserProgress (user_id, topic, day, completed)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, topic, day)
            DO UPDATE SET completed = excluded.completed
        ''', (user_id, topic, day, 1 if completed else 0))
        
        db.commit()
        return jsonify({"message": "Day completion status updated successfully"})
    except sqlite3.Error as e:
        # Log and return error if updating fails
        print("Database error:", e)
        return jsonify({"error": "Failed to update day completion status"}), 500

@app.route('/submit-story', methods=['POST'])
def submit_story():
    # Retrieve form data
    name = request.form.get('name')
    email = request.form.get('email')
    story = request.form.get('yourStory')
    image = request.files.get('image')

    # Basic validation to ensure required fields are present
    if not name or not email or not story:
        return jsonify({'error': 'All fields except image are required'}), 400

    # Process image if provided
    image_data = image.read() if image else None
    mime_type = image.mimetype if image else None

    # Insert story into the database
    db = get_db()
    db.execute("INSERT INTO stories (name, email, story, image, mime_type) VALUES (?, ?, ?, ?, ?)", 
               (name, email, story, image_data, mime_type))
    db.commit()

    return jsonify({"message": "Story submitted successfully!"}), 201

@app.route('/approved-stories', methods=['GET'])
def get_approved_stories():
    # Retrieve all approved stories from the database
    db = get_db()
    stories = db.execute("SELECT id, name, story, image, mime_type FROM stories WHERE approved = 1").fetchall()

    stories_data = []
    for story in stories:
        story_data = dict(story)
        if story_data['image']:
            # Encode image as base64 string with MIME type
            mime_type = story_data['mime_type'] or "image/jpeg"  # Default to JPEG if MIME type is not available
            story_data['image'] = f"data:{mime_type};base64," + base64.b64encode(story_data['image']).decode('utf-8')
        
        stories_data.append(story_data)

    return jsonify(stories_data)
    
@app.route('/story/<int:story_id>', methods=['GET'])
def get_story(story_id):
    # Retrieve specific approved story by ID from the database
    db = get_db()
    story = db.execute(
        "SELECT id, name, story, image, mime_type FROM stories WHERE id = ? AND approved = 1", (story_id,)
    ).fetchone()

    if story:
        # Convert the story to a dictionary format
        story_data = dict(story)
        
        # If an image exists, encode it as a base64 string with the correct MIME type
        if story_data['image']:
            mime_type = story_data['mime_type'] or "image/jpeg"  # Default to JPEG if MIME type is missing
            story_data['image'] = f"data:{mime_type};base64," + base64.b64encode(story_data['image']).decode('utf-8')
        
        return jsonify(story_data)
    else:
        return jsonify({"error": "Story not found"}), 404

def store_guide_in_db(topic, guide_data):
    # Store generated guide data in the database
    db = get_db()
    for day_item in guide_data["guide"]:
        db.execute('''
            INSERT OR IGNORE INTO GuideEntry (topic, day, title, approaches, completed)
            VALUES (?, ?, ?, ?, ?)
        ''', (
            topic,
            day_item["day"],
            day_item["title"],
            json.dumps(day_item["approaches"]),  # Store approaches as a JSON string
            0  # Default to not completed
        ))
    db.commit()


@app.route('/summarize-video', methods=['POST'])
def summarize_video():
    # Retrieve YouTube URL from request
    data = request.json
    youtube_url = data.get('url')
    
    # Validate if YouTube URL is provided
    if not youtube_url:
        return jsonify({"error": "YouTube URL is required"}), 400

    video_file_name = "/content/downloaded_video.mp4"

    try:
        # Step 1: Download the video from YouTube using yt-dlp
        print("Downloading video from YouTube...")
        download_command = ["yt-dlp", "-o", video_file_name, youtube_url]
        
        # Run the download command and check for errors
        result = subprocess.run(download_command, capture_output=True, text=True)
        if result.returncode != 0:
            print("Download error:", result.stderr)
            return jsonify({"error": "Failed to download video from YouTube"}), 500
        
        # Step 2: Upload the downloaded video to Generative AI (GenAI)
        print("Uploading file to GenAI...")
        video_file = genai.upload_file(path=video_file_name)

        # Step 3: Wait for GenAI to process the uploaded file
        print("Waiting for file processing to complete...")
        max_retries = 10  # Retry limit to prevent infinite loop
        retries = 0
        while video_file.state.name == "PROCESSING" and retries < max_retries:
            print('.', end='', flush=True)
            time.sleep(10)  # Wait before retrying
            video_file = genai.get_file(video_file.name)
            retries += 1

        # Handle upload failure or timeout
        if video_file.state.name == "FAILED" or retries == max_retries:
            raise ValueError("File upload failed or processing timed out")

        # Step 4: Generate video summary using the Generative AI model
        prompt = """
        Summarize this video clearly and concisely, capturing its main message, themes, and any key takeaways. Identify specific goals or outcomes that a viewer can work toward based on the video’s content.
        For each goal, provide a list of practical, actionable activities or exercises that will help achieve it. These should be realistic and achievable, designed to build momentum and reinforce progress.
        Use a coaching tone that encourages persistence, offers strategies to overcome challenges, and emphasizes the benefits of consistency and focus in working toward each goal.
        """

        print("Making LLM inference request...")
        response = model.generate_content([video_file, prompt], request_options={"timeout": 600})

        # Step 5: Return the generated summary as a response
        return jsonify({"summary": response.text})

    except Exception as e:
        # Handle exceptions and return error message
        print("Error:", e)
        return jsonify({"error": str(e)}), 500

    finally:
        # Step 6: Clean up by deleting the temporary video file
        if os.path.exists(video_file_name):
            os.remove(video_file_name)
            print("\nTemporary file deleted.")


def get_accountability_response3(user_input, chat_history=None):
    """Provides a supportive response for goal setting."""

    initial_prompt = """You are a super supportive and encouraging AI best friend for teenage girls aged 14-19.  You are enthusiastic, empowering, and understanding.
      You believe that girls and women are capable of achieving anything they set their minds to, regardless of societal expectations or stereotypes.
       You avoid gendered clichés and offer concrete, actionable advice where appropriate.  You understand the unique challenges faced by girls in this
       age group and provide a safe and supportive space for them to discuss their aspirations.  You help them break down their goals into smaller,
        manageable steps and celebrate their accomplishments, no matter how small. Remember to focus on building confidence and resilience.
         If the user expresses interest in a non-traditional career path for women, be extra encouraging and provide resources or examples of successful women in that field."""

    # Initialize chat history if it's empty
    if chat_history is None:
        chat_history = []

    # Add the initial prompt if chat history is empty
    if not chat_history:
        chat_history.append({"role": "user", "parts": initial_prompt})  # Important change
        chat_history.append({"role": "model", "parts": "Awesome! I'm so excited to hear about your goals and be your biggest cheerleader! ✨ Tell me everything – I'm here to listen, support you, and help you make those dreams a reality.  No dream is too big or too small!  Let's do this! 💪"})

    # Add the user's input to the chat history
    chat_history.append({"role": "user", "parts": user_input})

    # Start a chat session with the Generative AI model
    chat = model.start_chat(history=chat_history)
    response = chat.send_message(user_input)

    # Append the response to the chat history
    chat_history.append({"role": "model", "parts": response.text})

    return response.text, chat_history


@app.route('/chatbot', methods=['POST'])
def chatbot():
    # Retrieve user input from request
    user_input = request.json.get("message")
    chat_history = session.get("chat_history", [])

    # Generate accountability response based on user input and chat history
    response_text, updated_chat_history = get_accountability_response3(user_input, chat_history)

    # Update chat history in session
    session["chat_history"] = updated_chat_history

    # Return the chatbot's response
    return jsonify({"response": response_text})

@app.route('/generate-mentorship-guide', methods=['GET'])
def generate_management_guide():
    # Generate a list of 15 online resources where young women aged 14-19 can find mentors or networking opportunities
    prompt = '''List 15 online resources and platforms and URL links, aside from LinkedIn, where young women ages 14-19 can find mentors or networking opportunities. Tailor this list to support and inspire young women seeking guidance in their career journey.
    Provide specific platform names and a brief description of each, focusing on resources particularly suited to young women in this age range.'''
    response = model.generate_content(prompt)
    return response.text

@app.route('/generate-resources', methods=['GET'])
def generate_mentor_resources():
    # Generate a step-by-step guide on how to find a female mentor, including specific guidance and a sample message
    prompt = '''Create a step-by-step guide tailored for young women ages 14-19 on how to find a female mentor in manager or CEO roles through LinkedIn or online platforms. Use a friendly, supportive tone that feels both encouraging and approachable.
    Provide specific guidance for each of the following steps:
    1. Searching for potential mentors.
    2. Identifying and selecting a suitable mentor.
    3. Crafting a respectful, genuine message to initiate contact.
    Include a sample introductory message that is warm and conversational, suitable for reaching out to a potential mentor on LinkedIn.'''
    response = model.generate_content(prompt)
    return response.text

@app.route('/generate-scholarship-guide', methods=['GET'])
def generate_scholarship_guide():
    # Generate a step-by-step guide for finding college scholarships and tips for success in applying
    prompt = '''Create a step-by-step guide for finding college scholarships, followed by practical tips for success in applying. Tailor this guide to young women ages 14-19, focusing on inspiring and guiding them in their career journey.
    Include specific steps for researching scholarships, organizing applications, and preparing materials. Follow up with actionable tips to increase their chances of success.
    '''
    response = model.generate_content(prompt)
    return response.text

def main():
    # Run the Flask application
    app.run(port=int(os.environ.get('PORT', 5000)), debug=True)

# Entry point for running the script
if __name__ == "__main__":
    main()
