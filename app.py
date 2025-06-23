import os
import requests
import spacy
from datetime import datetime, timedelta
from flask import Flask, redirect, request, jsonify, render_template, session, flash, url_for
from flask_cors import CORS
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from dotenv import load_dotenv
from urllib.parse import quote
import google.generativeai as genai

load_dotenv()

app = Flask(__name__, template_folder="./templates")
app.secret_key = os.getenv("SECRET_KEY", "your-secret-key-change-in-production")
CORS(app)

# Flask-Login configuration
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access this page.'

# Facebook and Gemini configuration
FB_APP_ID = os.getenv("FB_APP_ID")
FB_APP_SECRET = os.getenv("FB_APP_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "AIzaSyC04udsaFEo_972M3uRr9l3KWO8PDmL_zQ")

# Initialize Gemini client
genai.configure(api_key=GEMINI_API_KEY)

# User class for Flask-Login
class User(UserMixin):
    def __init__(self, user_id, name, email=None, facebook_id=None, access_token=None):
        self.id = user_id
        self.name = name
        self.email = email
        self.facebook_id = facebook_id
        self.access_token = access_token
        self.pages = []

@login_manager.user_loader
def load_user(user_id):
    # In production, load from database
    if 'user_data' in session and session['user_data']['id'] == user_id:
        user_data = session['user_data']
        return User(
            user_data['id'],
            user_data['name'],
            user_data.get('email'),
            user_data.get('facebook_id'),
            user_data.get('access_token')
        )
    return None

@app.route("/", endpoint="home")
def index():
    return render_template("index.html")

# Step 1: Redirect user to Facebook login
@app.route("/login")
def login():
    if not REDIRECT_URI or not isinstance(REDIRECT_URI, str):
        flash("REDIRECT_URI is not set or invalid.", "error")
        return redirect(url_for('home'))
    encoded_uri = quote(str(REDIRECT_URI), safe="")
    fb_login_url = (
        f"https://www.facebook.com/v23.0/dialog/oauth?"
        f"client_id={FB_APP_ID}&"
        f"redirect_uri={encoded_uri}&"
        f"scope=pages_read_engagement,pages_read_user_content,email,public_profile"
    )
    return redirect(fb_login_url)

# Step 2: Handle Facebook redirect and get access token
@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        flash("Authorization failed. Please try again.", "error")
        return redirect(url_for('home'))

    if not REDIRECT_URI or not isinstance(REDIRECT_URI, str):
        flash("REDIRECT_URI is not set or invalid.", "error")
        return redirect(url_for('home'))
    encoded_uri = quote(str(REDIRECT_URI), safe="")
    token_url = (
        f"https://graph.facebook.com/v23.0/oauth/access_token?"
        f"client_id={FB_APP_ID}&"
        f"redirect_uri={encoded_uri}&"
        f"client_secret={FB_APP_SECRET}&"
        f"code={code}"
    )

    try:
        token_res = requests.get(token_url)
        token_res.raise_for_status()
        token_data = token_res.json()

        if 'access_token' in token_data:
            access_token = token_data['access_token']

            # Get user information
            user_url = f"https://graph.facebook.com/v23.0/me?access_token={access_token}&fields=id,name,email"
            user_res = requests.get(user_url)
            user_data = user_res.json()

            if 'id' in user_data:
                # Create user object and log them in
                user = User(
                    user_data['id'],
                    user_data['name'],
                    user_data.get('email'),
                    user_data['id'],
                    access_token
                )

                # Store user data in session
                session['user_data'] = {
                    'id': user_data['id'],
                    'name': user_data['name'],
                    'email': user_data.get('email'),
                    'facebook_id': user_data['id'],
                    'access_token': access_token
                }

                login_user(user)
                flash(f"Welcome, {user_data['name']}!", "success")
                return redirect(url_for('dashboard'))

        flash("Failed to authenticate with Facebook.", "error")
        return redirect(url_for('home'))

    except requests.exceptions.RequestException as e:
        flash(f"Authentication error: {str(e)}", "error")
        return redirect(url_for('home'))

@app.route("/logout")
@login_required
def logout():
    logout_user()
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for('home'))

@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html", user=current_user)

# Step 3: Get user pages
@app.route("/pages")
@login_required
def get_pages():
    try:
        access_token = session['user_data']['access_token']
        url = f"https://graph.facebook.com/v23.0/me/accounts?access_token={access_token}"
        res = requests.get(url)
        res.raise_for_status()
        pages_data = res.json()

        # Store pages in session for later use
        session['user_pages'] = pages_data.get('data', [])

        return jsonify(pages_data)
    except Exception as e:
        return jsonify({"error": f"Failed to fetch pages: {str(e)}"}), 500

# Step 4: Get posts from selected page with enhanced filtering
@app.route("/page-posts")
@login_required
def get_page_posts():
    try:
        page_id = request.args.get("page_id")
        page_token = request.args.get("page_token")
        topic = request.args.get("topic", "").lower()
        limit = request.args.get("limit", "50")

        if not page_id or not page_token:
            return jsonify({"error": "Missing page_id or page_token"}), 400

        url = (
            f"https://graph.facebook.com/v23.0/{page_id}/posts"
            f"?fields=message,created_time,id,likes.summary(true),comments.summary(true),shares"
            f"&limit={limit}"
            f"&access_token={page_token}"
        )

        res = requests.get(url)
        res.raise_for_status()
        posts_data = res.json()
        posts = posts_data.get("data", [])

        # Enhanced filtering with spaCy
        if topic and posts:
            nlp = spacy.load("en_core_web_sm")
            filtered_posts = []

            for post in posts:
                if "message" in post and post["message"]:
                    doc = nlp(post["message"])

                    # Multiple filtering strategies
                    topic_found = False

                    # 1. Direct keyword match
                    if topic in post["message"].lower():
                        topic_found = True

                    # 2. Lemmatized token match
                    elif any(token.lemma_.lower() == topic for token in doc):
                        topic_found = True

                    # 3. Semantic similarity (if entities match)
                    elif any(ent.text.lower() == topic for ent in doc.ents):
                        topic_found = True

                    if topic_found:
                        # Add engagement metrics
                        post['engagement_score'] = (
                            post.get('likes', {}).get('summary', {}).get('total_count', 0) +
                            post.get('comments', {}).get('summary', {}).get('total_count', 0) * 2 +
                            post.get('shares', {}).get('count', 0) * 3
                        )
                        filtered_posts.append(post)

            # Sort by engagement score
            filtered_posts.sort(key=lambda x: x.get('engagement_score', 0), reverse=True)

            return jsonify({
                "filtered_posts": filtered_posts,
                "total_filtered": len(filtered_posts),
                "total_original": len(posts),
                "topic": topic
            })

        return jsonify({"posts": posts, "total": len(posts)})

    except Exception as e:
        return jsonify({"error": f"Failed to fetch posts: {str(e)}"}), 500

# NEW: Generate blog post using Gemini
@app.route("/generate-blog", methods=["POST"])
@login_required
def generate_blog():
    try:
        if not GEMINI_API_KEY:
            return jsonify({"error": "Gemini API key not configured"}), 500

        data = request.get_json()
        posts = data.get('posts', [])
        topic = data.get('topic', 'Social Media Insights')
        blog_style = data.get('blog_style', 'informative')
        word_count = data.get('word_count', 800)

        if not posts:
            return jsonify({"error": "No posts provided for blog generation"}), 400

        # Prepare content from posts
        post_content = ""
        for i, post in enumerate(posts[:10], 1):  # Limit to top 10 posts
            if post.get('message'):
                post_content += f"\n\nPost {i}: {post['message'][:300]}..."
                if post.get('engagement_score'):
                    post_content += f" (Engagement Score: {post['engagement_score']})"

        # Create blog generation prompt
        prompt = f"""
        Create a {blog_style} blog post about \"{topic}\" based on the following Facebook posts. 

        Target word count: {word_count} words

        Facebook Posts Content:
        {post_content}

        Please create a well-structured blog post with:
        1. An engaging title
        2. Introduction
        3. Main body with insights and analysis
        4. Key takeaways or conclusions
        5. Call to action

        The blog should analyze trends, sentiments, and insights from the social media content.
        Make it engaging and informative for readers interested in {topic}.
        """

        # Call Gemini API
        model = genai.GenerativeModel('gemini-pro')
        response = model.generate_content(prompt)
        blog_content = response.text

        # Store the generated blog in session
        blog_data = {
            'content': blog_content,
            'topic': topic,
            'generated_at': datetime.now().isoformat(),
            'posts_analyzed': len(posts),
            'style': blog_style
        }

        session['generated_blog'] = blog_data

        return jsonify({
            "blog_content": blog_content,
            "metadata": {
                "topic": topic,
                "posts_analyzed": len(posts),
                "word_count": len(blog_content.split()),
                "style": blog_style
            }
        })

    except Exception as e:
        return jsonify({"error": f"Blog generation failed: {str(e)}"}), 500

# NEW: Blog management routes
@app.route("/blog-editor")
@login_required
def blog_editor():
    blog_data = session.get('generated_blog')
    if not blog_data:
        flash("No blog content found. Please generate a blog first.", "warning")
        return redirect(url_for('dashboard'))
    return render_template("blog_editor.html", blog_data=blog_data)

@app.route("/save-blog", methods=["POST"])
@login_required
def save_blog():
    try:
        data = request.get_json()
        content = data.get('content')
        title = data.get('title', 'Untitled Blog Post')

        if not content:
            return jsonify({"error": "No content to save"}), 400

        # In production, save to database
        blog_data = {
            'title': title,
            'content': content,
            'saved_at': datetime.now().isoformat(),
            'user_id': current_user.id
        }

        # For demo, store in session
        if 'saved_blogs' not in session:
            session['saved_blogs'] = []

        session['saved_blogs'].append(blog_data)
        session.modified = True

        return jsonify({"message": "Blog saved successfully", "blog_id": len(session['saved_blogs'])})

    except Exception as e:
        return jsonify({"error": f"Failed to save blog: {str(e)}"}), 500

@app.route("/my-blogs")
@login_required
def my_blogs():
    saved_blogs = session.get('saved_blogs', [])
    return render_template("my_blogs.html", blogs=saved_blogs)

# API endpoint for health check
@app.route("/health")
def health_check():
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "facebook_configured": bool(FB_APP_ID and FB_APP_SECRET),
        "gemini_configured": bool(GEMINI_API_KEY)
    })

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)