from flask import Flask, render_template, request, redirect, session, flash
import sqlite3
import math

app = Flask(__name__)
app.secret_key = "zerowaste_secret"

# ---------------- DATABASE INITIALIZATION ---------------- #

def init_db():
    conn = sqlite3.connect("database.db")
    c = conn.cursor()

    # USERS table - Added 'password' column
    c.execute('''CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE,
                    password TEXT,
                    role TEXT,
                    latitude REAL,
                    longitude REAL,
                    capacity INTEGER,
                    original_capacity INTEGER
                )''')

    # SURPLUS table
    c.execute('''CREATE TABLE IF NOT EXISTS surplus (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    restaurant_id INTEGER,
                    food_name TEXT,
                    quantity INTEGER,
                    expiry_hours INTEGER,
                    assigned_ngo_id INTEGER,
                    distance REAL,
                    status TEXT
                )''')

    conn.commit()
    conn.close()

init_db()

# ---------------- DISTANCE CALCULATION ---------------- #

def calculate_distance(lat1, lon1, lat2, lon2):
    R = 6371  # Earth radius in KM
    dLat = math.radians(lat2 - lat1)
    dLon = math.radians(lon2 - lon1)
    a = (math.sin(dLat/2)**2 +
         math.cos(math.radians(lat1)) *
         math.cos(math.radians(lat2)) *
         math.sin(dLon/2)**2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    return R * c

# ---------------- NGO MATCHING ---------------- #

def match_ngo(expiry_hours, quantity, restaurant_name):
    conn = sqlite3.connect("database.db")
    c = conn.cursor()

    c.execute("SELECT id, latitude, longitude FROM users WHERE name=?", (restaurant_name,))
    restaurant = c.fetchone()

    if not restaurant:
        conn.close()
        return None

    restaurant_id, r_lat, r_lon = restaurant
    c.execute("SELECT id, name, latitude, longitude, capacity FROM users WHERE role='ngo'")
    ngos = c.fetchall()

    best_score = None
    best_ngo = None

    for ngo in ngos:
        ngo_id, name, lat, lon, capacity = ngo
        if capacity >= quantity:
            distance = calculate_distance(r_lat, r_lon, lat, lon)
            
            # Smart scoring formula (Lower is better)
            # Prioritizes short distance and low expiry
            score = distance + (expiry_hours * 0.5)

            if best_score is None or score < best_score:
                best_score = score
                best_ngo = name

    conn.close()
    return best_ngo

# ---------------- ROUTES ---------------- #

@app.route("/")
def home():
    return render_template("home.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        role = request.form["role"]
        name = request.form["name"]
        password = request.form["password"]

        conn = sqlite3.connect("database.db")
        c = conn.cursor()

        # Check Name, Role, and Password
        c.execute("SELECT * FROM users WHERE name=? AND password=? AND role=?", (name, password, role))
        user = c.fetchone()
        conn.close()

        if user:
            session["user"] = name
            session["role"] = role
            return redirect("/dashboard" if role == "ngo" else "/add_surplus")
        else:
            return "Invalid credentials! Please check your name, password, or role."

    return render_template("login.html")

@app.route("/dashboard")
def dashboard():
    if "user" not in session or session["role"] != "ngo":
        return redirect("/login")

    ngo_name = session["user"]
    conn = sqlite3.connect("database.db")
    c = conn.cursor()

    c.execute("SELECT id, capacity, original_capacity FROM users WHERE name=?", (ngo_name,))
    ngo = c.fetchone()
    if not ngo:
        conn.close()
        return "NGO not found!"

    ngo_id, current_capacity, original_capacity = ngo
    used_percentage = ((original_capacity - current_capacity) / original_capacity * 100) if original_capacity > 0 else 0

    c.execute("""
        SELECT s.id, u.name, s.food_name, s.quantity, s.expiry_hours, s.distance, s.status
        FROM surplus s
        JOIN users u ON s.restaurant_id = u.id
        WHERE s.assigned_ngo_id = ?
    """, (ngo_id,))
    records = c.fetchall()

    # Stats for the Impact Dashboard
    c.execute("SELECT COUNT(*) FROM surplus")
    total_surplus = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM surplus WHERE status='Collected'")
    total_collected = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM surplus WHERE status='Assigned'")
    total_assigned = c.fetchone()[0]

    conn.close()
    return render_template("surplus.html", records=records, capacity=current_capacity, 
                           ngo_name=ngo_name, total_surplus=total_surplus, 
                           total_collected=total_collected, total_assigned=total_assigned,
                           used_percentage=round(used_percentage, 2))

@app.route("/add_surplus", methods=["GET", "POST"])
def add_surplus():
    if "user" not in session or session["role"] != "restaurant":
        return redirect("/login")

    if request.method == "POST":
        food_name = request.form["food_name"]
        quantity = int(request.form["quantity"])
        expiry = int(request.form["expiry"])

        conn = sqlite3.connect("database.db")
        c = conn.cursor()

        c.execute("SELECT id, latitude, longitude FROM users WHERE name=?", (session["user"],))
        res = c.fetchone()
        restaurant_id, r_lat, r_lon = res

        ngo_name = match_ngo(expiry, quantity, session["user"])
        if not ngo_name:
            conn.close()
            return "No NGO available with sufficient capacity!"

        c.execute("SELECT id, capacity, latitude, longitude FROM users WHERE name=?", (ngo_name,))
        ngo = c.fetchone()
        ngo_id, cap, n_lat, n_lon = ngo

        dist = calculate_distance(r_lat, r_lon, n_lat, n_lon)

        c.execute("INSERT INTO surplus (restaurant_id, food_name, quantity, expiry_hours, assigned_ngo_id, distance, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (restaurant_id, food_name, quantity, expiry, ngo_id, dist, "Assigned"))
        
        c.execute("UPDATE users SET capacity = capacity - ? WHERE id = ?", (quantity, ngo_id))
        
        conn.commit()
        conn.close()
        flash(f"Successfully assigned to {ngo_name} ({dist:.2f} KM away)!")
        return redirect("/add_surplus")

    return render_template("add_surplus.html")

@app.route("/collect/<int:surplus_id>")
def mark_collected(surplus_id):
    conn = sqlite3.connect("database.db")
    c = conn.cursor()

    # Logic: Mark as collected AND restore NGO capacity
    c.execute("SELECT quantity, assigned_ngo_id FROM surplus WHERE id=?", (surplus_id,))
    item = c.fetchone()
    
    if item:
        qty, ngo_id = item
        c.execute("UPDATE surplus SET status='Collected' WHERE id=?", (surplus_id,))
        c.execute("UPDATE users SET capacity = capacity + ? WHERE id=?", (qty, ngo_id))
        conn.commit()

    conn.close()
    return redirect("/dashboard")

@app.route("/register_ngo", methods=["GET", "POST"])
def register_ngo():
    if request.method == "POST":
        name = request.form["name"]
        password = request.form["password"]
        lat = float(request.form["latitude"])
        lon = float(request.form["longitude"])
        cap = int(request.form["capacity"])

        conn = sqlite3.connect("database.db")
        c = conn.cursor()
        c.execute("INSERT INTO users (name, password, role, latitude, longitude, capacity, original_capacity) VALUES (?, ?, 'ngo', ?, ?, ?, ?)",
                  (name, password, lat, lon, cap, cap))
        conn.commit()
        conn.close()
        return redirect("/login")
    return render_template("register_ngo.html")

@app.route("/register_restaurant", methods=["GET", "POST"])
def register_restaurant():
    if request.method == "POST":
        name = request.form["name"]
        password = request.form["password"]
        lat = float(request.form["latitude"])
        lon = float(request.form["longitude"])

        conn = sqlite3.connect("database.db")
        c = conn.cursor()
        c.execute("INSERT INTO users (name, password, role, latitude, longitude, capacity, original_capacity) VALUES (?, ?, 'restaurant', ?, ?, 0, 0)",
                  (name, password, lat, lon))
        conn.commit()
        conn.close()
        return redirect("/login")
    return render_template("register_restaurant.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

if __name__ == "__main__":
    app.run(debug=True)