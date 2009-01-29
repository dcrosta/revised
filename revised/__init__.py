import inspect

from django.contrib import admin
from django.db import models
from django.db.models.base import ModelBase
from django.db.models import signals

__all__ = ['RevisedModelBase','RevisedModel','RevisionAlreadyExists']

class NoSuchRevision(Exception):
    """
    Thrown when someone tries to revert to a revision
    which does not exist.
    """
    pass

class RevisionExists(Exception):
    """
    Thrown when someone tries to save a revision
    which already exists. Revisions are immutable.
    """
    pass

def RevisedSettings(name, attrs):
    """
    Create and return a "struct" class containing
    the merged default and user-specified fields
    for revision settings.
    """
    settings = {}
    settings['foreign_key_field_name'] = name.lower()
    settings['related_name'] = 'revisions'
    settings['revision_field_name'] = 'revision'
    settings['revision_model_name'] = '%sRevision' % name
    settings['unrevised_fields'] = tuple()
    settings['register_with_admin_site'] = False
    settings['RevisedAdmin'] = type('AdminSettings', tuple([admin.ModelAdmin]), {})
    settings = type('RevisedSettings', tuple(), settings)

    if 'Meta' in attrs:
        opts = attrs['Meta']
        for name in dir(settings):
            if name.startswith('_'):
                continue
            value = getattr(opts, name, False)
            if value:
                setattr(settings, name, value)
                delattr(opts, name)

    return settings

DEFAULT_ALLOWED_KWARGS = ['null','blank','choices','core','db_column','db_index','db_tablespace','default','editable','help_text','unique','unique_for_date','unique_for_month','unique_for_year','validator_list','name']
ALLOWED_KWARGS_BY_TYPE = {}

# initialize ALLOWED_KWARGS_BY_TYPE by scanning
# the args accepted by the __init__ of each of
# the *Field types defined in models
for field_name in dir(models):
    if not field_name.endswith('Field'):
        continue
    field_obj = getattr(models, field_name)
    init_func = getattr(field_obj, '__init__').im_func
    argspec = inspect.getargspec(init_func)
    argnames = list(argspec[0])
    argnames.remove('self')
    ALLOWED_KWARGS_BY_TYPE[field_name] = argnames

def filter_kwargs(kwargs, type):
    """
    Helper method used when creating the revision model. We
    pass in the __dict__ of the field from the original model,
    and filter it to contain only keys that are valid keyword
    arguments for the given field type.
    """
    new_kwargs = {}
    allowed_kwargs = list(DEFAULT_ALLOWED_KWARGS) + ALLOWED_KWARGS_BY_TYPE[type]
    for key, value in kwargs.items():
        if key not in allowed_kwargs:
            continue
        new_kwargs[key] = value
    return new_kwargs

def revision_model_save_factory(revision_cls):
    def revision_model_save(self):
        """
        Method that gets added to the *Revision models. It works
        as expected the first time a model is saved, and always
        raises an exception after that (so that old revisions
        cannot be edited)
        """
        if self.pk is None:
            super(revision_cls, self).save()
        else:
            settings = revision_cls._meta.revised_settings
            revision_num = getattr(self, settings.revision_field_name)
            instance_name = repr(self)
            raise RevisionExists('Revision %d already exists for %s' % (revision_num, instance_name))
    return revision_model_save

class RevisedModelBase(ModelBase):
    """
    Metaclass which does all the hard work of modifying the
    original model class and creating the revision model class,
    as described in RevisedModel's docstring.
    """

    def __new__(cls, name, bases, attrs):
        # must call this first, else the Meta
        # attr will have invalid options
        #
        # *** this modifies attrs! ***
        settings = RevisedSettings(name, attrs)

        # model is not an interesting class
        parents = [b for b in bases if isinstance(b, RevisedModelBase)]
        if not parents:
            return super(RevisedModelBase, cls).__new__(cls, name, bases, attrs)
        
        # add the extra field, make the class
        attrs[settings.revision_field_name] = models.PositiveIntegerField(default=1)
        model = super(RevisedModelBase, cls).__new__(cls, name, bases, attrs)

        new_attrs = {}
        revised_field_names = []
        opts = model._meta
        for field in opts._fields():
            if getattr(field, 'primary_key', False):
                continue
            if field.name in settings.unrevised_fields:
                continue
            field_type_name = field.__class__.__name__
            kwargs = filter_kwargs(field.__dict__, field_type_name)
            field_type = type(field)
            new_field = field_type(**kwargs)
            new_attrs[field.name] = new_field
            revised_field_names.append(field.name)

        new_attrs['__module__'] = model.__module__
        new_attrs[settings.foreign_key_field_name] = models.ForeignKey(model, related_name=settings.related_name)
        new_attrs[settings.revision_field_name] = models.PositiveIntegerField()

        # make the Meta class
        meta_attrs = {}
        meta_attrs['db_table'] = opts.db_table + '_revision'
        meta_attrs['ordering'] = ['-%s' % settings.revision_field_name]
        meta_attrs['unique_together'] = [(settings.foreign_key_field_name, settings.revision_field_name)]
        new_attrs['Meta'] = type('Meta', tuple(), meta_attrs)

        # make the revision model class
        revision_model = ModelBase(
                settings.revision_model_name,
                tuple([models.Model]),
                new_attrs
            )

        # copy methods defined in the model class
        model_dict = dict(model.__dict__)
        revision_model_dict = revision_model.__dict__
        for name in model_dict:
            # don't overwrite methods that
            # automatically exist
            if name not in revision_model_dict:
                setattr(revision_model, name, model_dict[name])
        setattr(revision_model, 'save', revision_model_save_factory(revision_model))

        # save settings for later use
        setattr(settings, 'revised_model_cls', revision_model)
        setattr(settings, 'revised_field_names', revised_field_names)
        setattr(opts, 'revised_settings', settings)
        setattr(revision_model._meta, 'revised_settings', settings)

        # make the revision model available in the module
        # along with the original model
        module = __import__(model.__module__, {}, {}, [''])
        setattr(module, settings.revision_model_name, revision_model)
        if hasattr(module, '__all__'):
            if type(module.__all__) == type(list()):
                module.__all__.append(settings.revision_model_name)
            elif type(module.__all__) == type(tuple()):
                all = list(module.__all__)
                all.append(settings.revision_model_name)
                setattr(module, '__all__', tuple(all))

        if settings.register_with_admin_site:
            admin.site.register(revision_model, settings.RevisedAdmin)

        return model


class RevisedModel(models.Model):
    """
    A Model superclass that extends Django's Model to add
    revision-storing capabilities to an otherwise ordinary
    model. This class will add one field to the model, and
    create an additional model class to store the revisions.
    The dynamically created model will have all the same
    fields as the original model, except the primary key,
    and will have an additional ForeignKey reference to the
    instance of the original model it is a revision of.

    Attributes of the original model's Meta class control the
    names of the fields created by this metaclass:

     * foreign_key_field_name
       - defaults to the original model name in lowercase
     * related_name
       - defaults to 'revisions'
     * revision_field_name (of both the original and new model)
       - defaults to 'revision'
     * revision_model_name
       - defaults to the original model name with 'Revision'
         appended
    """
    __metaclass__ = RevisedModelBase

    class Meta:
        abstract = True

    @property
    def __revised_model_cls(self):
        return self._meta.revised_settings.revised_model_cls

    def __setting(self, setting_name):
        return getattr(self._meta.revised_settings, setting_name)

    def __changed(self):
        """
        Return true if any revision-managed fields in this
        instance are different from revision-managed fields
        in the `other` instance.
        """
        for key, value in self._revised_initial_values.items():
            if getattr(self, key) != value:
                return True
        return False

    def __save_revision(self):
        """
        Save the model's initial values as a new revision,
        and return the revision number that was just saved.
        """
        kwargs = self._revised_initial_values
        revision = self.__revised_model_cls(**kwargs)
        self.__foreign_key_field = self
        setattr(revision, self.__setting('foreign_key_field_name'), self)
        revision.save()
        curr_revision = kwargs[self.__setting('revision_field_name')]
        return curr_revision

    def save(self):
        """
        If the model has been changed since it was loaded
        from the database, save the old version to the
        revision table, increment this instance's revision
        field, and save this revision. Otherwise, save
        as normal for models (though there's not much point
        if nothing changed...)
        """
        if self.pk is not None and self.__changed():
            # then we save a revision
            just_saved_revision = self.__save_revision()
            setattr(self, self.__setting('revision_field_name'), just_saved_revision + 1)
        super(RevisedModel, self).save()

    def delete(self, deep=False):
        """
        If deep is True, delete the model as usual and also
        delete all revisions linked to this instance.
        Otherwise, save the current instance as a revision,
        then delete it.
        """
        if deep:
            revisions = getattr(self, self.__setting('related_name'))
            for x in revisions.all():
                x.delete()
        else:
            self.__save_revision()
        super(RevisedModel, self).delete()

    def revert_to_revision(self, revision_to_delete):
        """
        Update the current revision (self) to have the contents
        of revised fields as saved in the given old revision, but
        do not save the model; this is left to the caller.
        """
        revisions = getattr(self, self.__setting('related_name'))
        getter_kwarg = {self.__setting('revision_field_name'): revision_to_delete}
        old_revision = revisions.get(**getter_kwarg)

        unrevised_fields = self.__setting('unrevised_fields')

        for field in self.__class__._meta._fields():
            if getattr(field, 'primary_key', False):
                continue
            if field.name in unrevised_fields:
                continue
            name = field.name
            setattr(self, name, getattr(old_revision, name))

def record_model_values(sender, instance, *args, **kwargs):
    """
    Store the values of this object when it was created, either
    as a new instance, or after an instance is retrieved from
    the database. This lets us check if the instance has changed
    without having to make a new database query.
    
    (See RevisedModel.__changed)
    """
    if not issubclass(sender, RevisedModel):
        return
    settings = instance._meta.revised_settings
    initial_values = {}
    for field_name in settings.revised_field_names:
        initial_values[field_name] = getattr(instance, field_name)
    setattr(instance, '_revised_initial_values', initial_values)
signals.post_init.connect(record_model_values)
signals.post_save.connect(record_model_values)

