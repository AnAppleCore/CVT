from models.utils.continual_model import ContinualModel


class OViLa(ContinualModel):
    NAME = 'ovila'
    COMPATIBILITY = ['class-il', 'domiain-il', 'task-il', 'general-continual']