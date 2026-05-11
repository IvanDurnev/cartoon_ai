from datetime import datetime

from extensions import db


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    children = db.relationship("Child", backref="creator", lazy=True)
    cartoons = db.relationship("Cartoon", backref="creator", lazy=True)


class Child(db.Model):
    __tablename__ = "children"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    photo_filename = db.Column(db.String(255), nullable=False)
    voice_id = db.Column(db.String(120), nullable=True)  # ElevenLabs voice id
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    avatars = db.relationship(
        "CartoonAvatar", backref="child", lazy=True, cascade="all, delete-orphan"
    )

    @property
    def selected_avatar(self):
        return next((a for a in self.avatars if a.is_selected), None)

    @property
    def has_pending_generation(self):
        return any(a.status == "pending" for a in self.avatars)


class Character(db.Model):
    __tablename__ = "characters"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=True)
    image_filename = db.Column(db.String(255), nullable=True)
    voice_id = db.Column(db.String(120), nullable=True)  # ElevenLabs voice id
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class CartoonAvatar(db.Model):
    __tablename__ = "cartoon_avatars"

    id = db.Column(db.Integer, primary_key=True)
    child_id = db.Column(db.Integer, db.ForeignKey("children.id"), nullable=False)
    task_id = db.Column(db.String(100))  # задача в aivideoapi
    status = db.Column(db.String(20), default="pending")  # pending | completed | failed
    image_url = db.Column(db.String(500))  # URL готового изображения
    style_name = db.Column(db.String(50))  # Disney / Pixar / Аниме
    is_selected = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "task_id": self.task_id,
            "status": self.status,
            "image_url": self.image_url,
            "style_name": self.style_name,
            "is_selected": self.is_selected,
        }


class Cartoon(db.Model):
    __tablename__ = "cartoons"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200))
    story_prompt = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), default="draft")  # draft | generating | ready | failed
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    scenes = db.relationship(
        "CartoonScene",
        backref="cartoon",
        cascade="all, delete-orphan",
        order_by="CartoonScene.scene_number",
    )
    participants = db.relationship(
        "CartoonParticipant", backref="cartoon", cascade="all, delete-orphan"
    )
    character_links = db.relationship(
        "CartoonCharacterLink", backref="cartoon", cascade="all, delete-orphan"
    )

    @property
    def total_duration(self):
        return sum(s.duration_seconds or 0 for s in self.scenes)


class CartoonParticipant(db.Model):
    __tablename__ = "cartoon_participants"

    id = db.Column(db.Integer, primary_key=True)
    cartoon_id = db.Column(db.Integer, db.ForeignKey("cartoons.id"), nullable=False)
    child_id = db.Column(db.Integer, db.ForeignKey("children.id"), nullable=False)
    child = db.relationship("Child")


class CartoonCharacterLink(db.Model):
    __tablename__ = "cartoon_character_links"

    id = db.Column(db.Integer, primary_key=True)
    cartoon_id = db.Column(db.Integer, db.ForeignKey("cartoons.id"), nullable=False)
    character_id = db.Column(db.Integer, db.ForeignKey("characters.id"), nullable=False)
    character = db.relationship("Character")


class CartoonScene(db.Model):
    __tablename__ = "cartoon_scenes"

    id = db.Column(db.Integer, primary_key=True)
    cartoon_id = db.Column(db.Integer, db.ForeignKey("cartoons.id"), nullable=False)
    scene_number = db.Column(db.Integer, nullable=False)
    title = db.Column(db.String(200))
    description = db.Column(db.Text)
    visual_description = db.Column(db.Text)
    duration_seconds = db.Column(db.Integer)
    dialogue = db.Column(db.Text)
    weather = db.Column(db.String(300))
    music = db.Column(db.String(300))
    sound_effects = db.Column(db.String(300))
    facial_expressions = db.Column(db.Text)
    # video generation
    video_prompt = db.Column(db.Text)  # English prompt for aivideoapi
    video_task_id = db.Column(db.String(100))
    video_status = db.Column(db.String(20))  # pending | completed | failed
    video_url = db.Column(db.String(500))

    def to_dict(self):
        return {
            "id": self.id,
            "cartoon_id": self.cartoon_id,
            "scene_number": self.scene_number,
            "video_status": self.video_status,
            "video_url": self.video_url,
        }
