"""
Variables
---------
Module description
"""
from abc import ABC, abstractmethod
import operator
import numbers
import collections

import chainer
import chainer.links as L
import chainer.functions as F
import numpy as np

from brancher.utilities import join_dicts_list, join_sets_list
from brancher.utilities import flatten_list
from brancher.utilities import partial_broadcast
from brancher.utilities import coerce_to_dtype
from brancher.utilities import broadcast_parent_values
from brancher.utilities import split_dict
from brancher.utilities import reformat_sampler_input
from brancher.utilities import tile_parameter

from brancher.pandas_interface import reformat_sample_to_pandas
from brancher.pandas_interface import reformat_model_summary
from brancher.pandas_interface import pandas_frame2dict
from brancher.pandas_interface import pandas_frame2value

class BrancherClass(ABC):
    """
    BrancherClass is the abstract superclass of all Brancher variables and models.
    """
    @abstractmethod
    def _flatten(self):
        """
        Abstract method. It returs a list of all the variables contained in the model.
        """
        pass

    def flatten(self):
        return set(self._flatten())

    def get_variable(self, var_name):
        """
        It returns the variable in the model with the requested name.

        Args:
            var_name: String. Name  of the requested variable.

        Returns:
            brancher.Variable.

        """
        flat_list = self._flatten()
        try:
            return {var.name: var for var in flat_list}[var_name]
        except ValueError:
            raise ValueError("The variable {} is not present in the model".format(var_name))


class Variable(BrancherClass):
    """
    Variable is the abstract superclass of deterministic and random variables. Variables are the building blocks of
    all probabilistic models in Brancher.
    """
    @abstractmethod
    def calculate_log_probability(self, values, reevaluate):
        """
        Abstract method. It returns the log probability of the values given the model.

        Args:
            values: Dictionary(brancher.Variable: chainer.Variable). A dictionary having the brancher.variables of the
            model as keys and chainer.Variables as values. This dictionary has to provide values for all variables of
            the model except for the deterministic variables.

            reevaluate: Bool. If false it returns the output of the latest call. It avoid unnecessary computations when
            multiple children variables ask for the log probability of the same paternt variable.

        Returns:
            chainer.Variable. the log probability of the input values given the model.

        """
        pass

    @abstractmethod
    def _get_sample(self, number_samples, resample, observed, input_values):
        """
        Abstract method. It returns samples from the joint distribution specified by the model. If an input is provided
        it only samples the variables that are not contained in the input.

        Args:
            number_samples: Int.

            resample: Bool. If true it returns its previously stored sampled. It is used when multiple children variables
            ask for a sample to the same parent. In this case the resample variable is False since the children should be
            fed with the same value of the parent.

            observed: Bool. It specifies whether the samples should be interpreted frequentistically as samples from the
            observations of as Bayesian samples from the prior model. The first batch dimension is reserved to Bayesian
            samples while the second batch dimension is reserved to observation samples. Theerefore, this boolean
            changes the shape of the resulting sampled array.

            input_values: Dictionary(brancher.Variable, chainer.Variable). A dictionary having the brancher.variables of the
            model as keys and chainer.Variables as values. This dictionary has to provide values for all variables of
            the model that do not need to be sampled. Using an input allows to use a probabilistic model as a random
            function.

        Returns:
            Dictionary(brancher.Variable: chainer.Variable). A dictionary of samples from all the variables of the model

        """
        pass

    def get_sample(self, number_samples, input_values={}):
        reformatted_input_values = reformat_sampler_input(pandas_frame2dict(input_values),
                                                          number_samples=number_samples)
        raw_sample = {self: self._get_sample(number_samples, resample=False,
                                             observed=self.is_observed, input_values=reformatted_input_values)[self]}
        sample = reformat_sample_to_pandas(raw_sample, number_samples)
        self.reset()
        return sample

    @abstractmethod
    def reset(self):
        """
        Abstract method. It recursively reset the self._evalueted and self._current_value attributes of the variable and
        all downstream variables. It is used after sampling and evaluating the log probability of a model.

        Args: None.

        Returns: None.
        """
        pass

    @property
    @abstractmethod
    def is_observed(self):
        """
        Abstract property method. It returns True if the variable is observed and False otherwise.

        Args: None.

        Returns: Bool.
        """
        pass

    def __str__(self):
        """
        Method.

        Args: None

        Returns: String
        """
        return self.name

    def _apply_operator(self, other, op):
        """
        Method. It is used for using operations between variables symbolically. It always returns a partialLink object
        that define a mathematical operation between variables. The vars attribute of the link is the set of variables
        that are used in the operation. The fn attribute is a lambda that specify the operation as a functions between the
        values of the variables in vars and a numeric output. This is required for defining the forward pass of the model.

        Args:
            other: PartialLink, RandomVariable, numeric or np.array.

            op: Binary operator.

        Returns: PartialLink
        """
        if isinstance(other, PartialLink):
            vars = other.vars
            vars.add(self)
            fn = lambda values: op(values[self], other.fn(values))
            links = other.links
        elif isinstance(other, Variable):
            vars = {self, other}
            fn = lambda values: op(values[self], values[other])
            links = set()
        elif isinstance(other, (numbers.Number, np.ndarray)):
            vars = {self}
            fn = lambda values: op(values[self], other)
            links = set()
        else:
            raise TypeError('') #TODO

        return PartialLink(vars=vars, fn=fn, links=links)

    def __add__(self, other):
        return self._apply_operator(other, operator.add)

    def __radd__(self, other):
        return self.__add__(other)

    def __sub__(self, other):
        return self._apply_operator(other, operator.sub)

    def __rsub__(self, other):
        return -1*self.__sub__(other)

    def __mul__(self, other):
        return self._apply_operator(other, operator.mul)

    def __rmul__(self, other):
        return self.__mul__(other)

    def __truediv__(self, other):
        return self._apply_operator(other, operator.truediv)

    def __rtruediv__(self, other):
        raise NotImplementedError

    def __pow__(self, other):
        return self._apply_operator(other, operator.pow)

    def __rpow__(self, other):
        raise NotImplementedError

    def __getitem__(self, key):
        if isinstance(key, collections.Iterable):
            variable_slice = (slice(None, None, None), *key)
        else:
            variable_slice = (slice(None, None, None), key)
        vars = {self}
        fn = lambda values: values[self][variable_slice]
        links = set()
        return PartialLink(vars=vars, fn=fn, links=links)

    def shape(self):
        vars = {self}
        fn = lambda values: values[self].shape
        links = set()
        return PartialLink(vars=vars, fn=fn, links=links)


class DeterministicVariable(Variable):
    """
    Deterministic variables are a subclass of random variables that always return the same value. The hyper-parameters of
    a probabilistic model are usually encoded as DeterministicVariables. When the user input a parameter as a Numeric value or
    an array, Brancher created a DeterministicVariable that store its value.

    Parameters
    ----------
    data : chainer.Variable, numeric, or np.ndarray. The value of the variable. It gets stored in the self.current value
    attribute.

    name : String. The name of the variable.

    learnable : Bool. This boolean value specify if the value of the DeterministicVariable can be updated during treaning.

    """
    def __init__(self, data, name, learnable=False, is_observed=False):
        self._current_value = coerce_to_dtype(data, is_observed)
        self.name = name
        self._observed = is_observed
        self.parents = ()
        self._type = "Deterministic"
        self.learnable = learnable
        if learnable:
            self.link = L.Bias(axis=1, shape=self._current_value.shape[1:])

    def calculate_log_probability(self, values, reevaluate=True):
        """
        Method. It returns the log probability of the values given the model. This value is always 0 since the probability
        of a deterministic variable having its value is always 1.

        Args:
            values: Dictionary(brancher.Variable: chainer.Variable). A dictionary having the brancher.variables of the
            model as keys and chainer.Variables as values. This dictionary has to provide values for all variables of
            the model except for the deterministic variables.

            reevaluate: Bool. If false it returns the output of the latest call. It avoid unnecessary computations when
            multiple children variables ask for the log probability of the same paternt variable.

        Returns:
            chainer.Variable. the log probability of the input values given the model.
        """
        return 0.

    @property
    def value(self):
        assert self._current_value is not None
        if self.learnable:
            return self.link(self._current_value)
        return self._current_value

    @value.setter
    def value(self, val):
        self._current_value = coerce_to_dtype(val, is_observed=self.is_observed)

    @property
    def is_observed(self):
        return self._observed

    def _get_sample(self, number_samples, resample=False, observed=False, input_values={}):
        if self in input_values:
            value = input_values[self]
        else:
            value = self.value
        if isinstance(value, chainer.Variable):
            return {self: tile_parameter(value, number_samples=number_samples)}
            #value_shape = value.shape
            #reps = tuple([number_samples] + [1]*len(value_shape[1:]))
            #return {self: F.tile(value, reps=reps)} #TODO: Work in progress
        else:
            return {self: value} #TODO: This is for allowing discrete data, temporary?

    def reset(self):
        pass

    def _flatten(self):
        return [self]


class RandomVariable(Variable):
    """
    Random variables are the main building blocks of probabilistic models.

    Parameters
    ----------
    distribution : brancher.Distribution
        Summary
    name : str
        Summary
    parents : tuple of brancher variables
        Summary
    link : callable
        Summary
    """
    def __init__(self, distribution, name, parents, link):
        self.name = name
        self.distribution = distribution
        self.link = link
        self.parents = parents
        self._type = "Random"
        self.samples = []

        self._evaluated = False
        self._observed = False
        self._observed_value = None
        self._current_value = None
        self.dataset = None
        self.has_random_dataset = False
        self.has_observed_value = False

    @property
    def value(self):
        if self._observed:
            return self._observed_value
        return self._current_value

    @value.setter
    def value(self, val):
        self._current_value = coerce_to_dtype(val)

    @property
    def is_observed(self):
        return self._observed

    def _apply_link(self, parents_values):  #TODO: This is for allowing discrete data, temporary?
        cont_values, discrete_values = split_dict(parents_values,
                                                  condition=lambda key, val: isinstance(val, chainer.Variable))
        if cont_values:
            reshaped_dict, number_samples, number_datapoints = broadcast_parent_values(cont_values)
            reshaped_dict.update(discrete_values)
        else:
            reshaped_dict = discrete_values
        reshaped_output = self.link(reshaped_dict)
        output = {key: F.reshape(val, (number_samples, number_datapoints) + val.shape[1:])
                  if isinstance(val, chainer.Variable) else val
                  for key, val in reshaped_output.items()}
        return output

    def calculate_log_probability(self, input_values, reevaluate=True):
        """
        Method. It returns the log probability of the values given the model. This value is always 0 since the probability
        of a deterministic variable having its value is always 1.

        Args:
            values: Dictionary(brancher.Variable: chainer.Variable). A dictionary having the brancher.variables of the
            model as keys and chainer.Variables as values. This dictionary has to provide values for all variables of
            the model except for the deterministic variables.

            reevaluate: Bool. If false it returns the output of the latest call. It avoid unnecessary computations when
            multiple children variables ask for the log probability of the same paternt variable.

        Returns:
            chainer.Variable. the log probability of the input values given the model.

        """
        if self._evaluated and not reevaluate:
            return 0.
        if self in input_values:
            value = input_values[self]
        else:
            value = self.value

        self._evaluated = True
        deterministic_parents_values = {parent: parent.value for parent in self.parents
                                        if (type(parent) is DeterministicVariable)}
        parents_input_values = {parent: parent_input for parent, parent_input in input_values.items() if parent in self.parents}
        parents_values = {**parents_input_values, **deterministic_parents_values}
        parameters_dict = self._apply_link(parents_values)
        log_probability = self.distribution.calculate_log_probability(value, **parameters_dict)
        parents_log_probability = sum([parent.calculate_log_probability(input_values, reevaluate) for parent in self.parents])
        if self.is_observed:
            log_probability = F.sum(log_probability, axis=1, keepdims=True)
        if type(log_probability) is chainer.Variable and type(parents_log_probability) is chainer.Variable:
            log_probability, parents_log_probability = partial_broadcast(log_probability, parents_log_probability)
        return log_probability + parents_log_probability

    def _get_sample(self, number_samples=1, resample=True, observed=False, input_values={}):
        """
        Summary
        """
        if self.samples and not resample:
            return {self: self.samples[-1]}
        if not observed:
            if self in input_values:
                return {self: input_values[self]} #TODO: This breaks the recursion if an input is provided. The future will decide if this is a feature or a bug!
            else:
                var_to_sample = self
        else:
            if self.has_observed_value:
                return {self: self._observed_value}
            elif self.has_random_dataset:
                var_to_sample = self.dataset
            else:
                var_to_sample = self
        parents_samples_dict = join_dicts_list([parent._get_sample(number_samples, resample, observed, input_values)
                                                for parent in var_to_sample.parents])
        input_dict = {parent: parents_samples_dict[parent] for parent in var_to_sample.parents}
        parameters_dict = var_to_sample._apply_link(input_dict)
        sample = var_to_sample.distribution.get_sample(**parameters_dict, number_samples=number_samples)
        self.samples.append(sample)
        return {**parents_samples_dict, self: sample}

    def observe(self, data, random_indices=()):
        """
        Summary
        """
        data = pandas_frame2value(data, self.name)
        if isinstance(data, RandomVariable):
            self.dataset = data
            self.has_random_dataset = True
        else:
            self._observed_value = coerce_to_dtype(data, is_observed=True)
            self.has_observed_value = True
        self._observed = True

    def unobserve(self):
        self._observed = False
        self.has_observed_value = False
        self.has_random_dataset = False
        self._observed_value = None
        self.dataset = None

    def reset(self):
        """
        Summary
        """
        self.samples = []
        self._evaluated = False
        self._current_value = None
        for parent in self.parents:
            parent.reset()

    def _flatten(self):
        return flatten_list([parent._flatten() for parent in self.parents]) + [self]


class ProbabilisticModel(BrancherClass):
    """
    Summary

    Parameters
    ----------
    variables : tuple of brancher variables
        Summary
    """
    def __init__(self, variables):
        self.variables = self._validate_variables(variables)
        self._set_summary()
        self.posterior_model = None
        self.observed_submodel = None
        self.diagnostics = {}
        if not all([var.is_observed for var in self.variables]): #TODO: this is not elegant
            self.update_observed_submodel()
        else:
            self.observed_submodel = self

    def __str__(self): #TODO: Work in progress
        """
        Method.

        Args: None

        Returns: String
        """
        return self.model_summary.__str__()

    @staticmethod
    def _validate_variables(variables):
        for var in variables:
            if not isinstance(var, (DeterministicVariable, RandomVariable)):
                raise ValueError("Invalid input type: {}".format(type(var)))
        return variables

    def _set_summary(self): #TODO: Work in progress
        feature_list = ["Distribution", "Parents", "Observed"]
        var_list = self.flatten()
        var_names = [var.name for var in var_list]
        summary_data = [[var._type, var.parents, var.is_observed]
                         for var in var_list]
        self._model_summary = reformat_model_summary(summary_data, var_names, feature_list)

    @property
    def model_summary(self):
        self._set_summary()
        return self._model_summary

    @property
    def value(self):
        return tuple(var.value for var in self.variables)

    @value.setter
    def value(self, val):
        raise AttributeError("The value of a probabilistic model cannot be explicitly set.")

    @property
    def is_observed(self):
        return all([var.is_observed for var in self._flatten()])

    def update_observed_submodel(self):
        """
        Summary

        Parameters
        ---------
        """
        flattened_model = self._flatten()
        observed_variables = [var for var in flattened_model if var.is_observed]
        self.observed_submodel = ProbabilisticModel(observed_variables)

    def set_posterior_model(self, model):
        self.posterior_model = PosteriorModel(posterior_model=model, joint_model=self)

    def calculate_log_probability(self, rv_values):
        """
        Summary
        """
        log_probability = sum([var.calculate_log_probability(rv_values, reevaluate=False) for var in self.variables])
        self.reset()
        return log_probability

    def _get_sample(self, number_samples, observed=False, input_values={}):
        """
        Summary
        """
        joint_sample = join_dicts_list([var._get_sample(number_samples=number_samples, resample=False,
                                                        observed=observed, input_values=input_values)
                                        for var in self.variables])
        joint_sample.update(input_values)
        self.reset()
        return joint_sample

    def get_sample(self, number_samples, input_values={}):
        reformatted_input_values = reformat_sampler_input(pandas_frame2dict(input_values),
                                                                            number_samples=number_samples)
        raw_sample = self._get_sample(number_samples, observed=False, input_values=reformatted_input_values)
        sample = reformat_sample_to_pandas(raw_sample, number_samples=number_samples)
        return sample

    def check_posterior_model(self):
        """
        Summary
        """
        if not self.posterior_model:
            raise AttributeError("The posterior model has not been initialized.")
#        elif self.posterior_model._is_trained is False:
#            raise AttributeError("The posterior model needs to be trained before sampling.")

    def _get_posterior_sample(self, number_samples, input_values={}):
        """
        Summary
        """
        self.check_posterior_model()
        posterior_sample = self.posterior_model._get_posterior_sample(number_samples=number_samples,
                                                                      input_values=input_values)
        sample = self._get_sample(number_samples, input_values=posterior_sample)
        return sample

    def get_posterior_sample(self, number_samples, input_values={}): #TODO: Work in progress
        reformatted_input_values = reformat_sampler_input(pandas_frame2dict(input_values),
                                                                            number_samples=number_samples)
        raw_sample = self._get_posterior_sample(number_samples, input_values=reformatted_input_values)
        sample = reformat_sample_to_pandas(raw_sample, number_samples=number_samples)
        return sample

    def estimate_log_model_evidence(self, number_samples, method="ELBO", input_values={}):  #TODO Work in progress
        self.check_posterior_model()
        if method is "ELBO":
            samples = self.observed_submodel._get_sample(1, observed=True) #TODO: You need to correct for subsampling
            posterior_samples = self.posterior_model._get_sample(number_samples=number_samples,
                                                                 observed=False, input_values=input_values)
            posterior_log_prob = self.posterior_model.calculate_log_probability(posterior_samples)
            samples.update(self.posterior_model.posterior_sample2joint_sample(posterior_samples))
            joint_log_prob = self.calculate_log_probability(samples)
            log_model_evidence = F.mean(joint_log_prob - posterior_log_prob) #TODO: It was sum, bug?
            return log_model_evidence
        else:
            raise NotImplementedError("The requested estimation method is currently not implemented.")

    def reset(self):
        """
        Summary
        """
        for variable in self.variables:
            variable.reset()

    def _flatten(self):
        return flatten_list([var._flatten() for var in self.variables])


class PosteriorModel(ProbabilisticModel):
    """
    Summary

    Parameters
    ----------
    variables : tuple of brancher variables
        Summary
    """
    def __init__(self, posterior_model, joint_model):
        super().__init__(posterior_model.variables)
        self.posterior_model = None
        self.model_mapping = self.set_model_mapping(joint_model)

        self._is_trained = False

    def set_model_mapping(self, joint_model):
        model_mapping = {}
        for p_var in joint_model._flatten():
            try:
                model_mapping.update({self.get_variable(p_var.name): p_var})
            except KeyError:
                pass
                # if p_var.is_observed or type(p_var) is DeterministicVariable:
                #     pass
                # else:
                #     raise ValueError(
                #         "The variable {} is not present in the variational distribution".format(p_var.name))
        return model_mapping

    def posterior_sample2joint_sample(self, posterior_sample):
        joint_sample = {}
        for key, value in posterior_sample.items():
            try:
                joint_sample.update({self.model_mapping[key]: value})
            except KeyError:
                pass
        return joint_sample

    def _get_posterior_sample(self, number_samples, observed=False, input_values={}):
        sample = self.posterior_sample2joint_sample(self._get_sample(number_samples, observed, input_values))
        sample.update(input_values)
        return sample


def var2link(var):
    if isinstance(var, Variable):
        vars = {var}
        fn = lambda values: values[var]
    elif isinstance(var, (numbers.Number, np.ndarray)):
        vars = {}
        fn = lambda values: var
    elif isinstance(var, tuple) and all([isinstance(v, (Variable, PartialLink)) for v in var]):
        vars = join_sets_list([{v} if isinstance(v, Variable) else v.vars for v in var])
        fn = lambda values: tuple([values[v] if isinstance(v, Variable) else v.fn(values) for v in var])
    else:
        return var
    return PartialLink(vars=vars, fn=fn, links=set())


class PartialLink(BrancherClass): #TODO: This should become "ProbabilisticProgram?"

    def __init__(self, vars, fn, links):
        self.vars = vars
        self.fn = fn
        self.links = links

    def _apply_operator(self, other, op):
        other = var2link(other)
        return PartialLink(vars=self.vars.union(other.vars),
                           fn=lambda values: op(self.fn(values), other.fn(values)),
                           links=self.links.union(other.links))

    def __add__(self, other):
        return self._apply_operator(other, operator.add)

    def __radd__(self, other):
        return self.__add__(other)

    def __sub__(self, other):
        return self._apply_operator(other, operator.sub)

    def __rsub__(self, other):
        return -1*self.__sub__(other)

    def __mul__(self, other):
        return self._apply_operator(other, operator.mul)

    def __rmul__(self, other):
        return self.__mul__(other)

    def __truediv__(self, other):
        return self._apply_operator(other, operator.truediv)

    def __rtruediv__(self, other):
        raise NotImplementedError

    def __pow__(self, other):
        return self._apply_operator(other, operator.pow)

    def __rpow__(self, other):
        raise NotImplementedError

    def __getitem__(self, key):
        if isinstance(key, collections.Iterable):
            variable_slice = (slice(None, None, None), *key)
        else:
            variable_slice = (slice(None, None, None), key)
        vars = self.vars
        fn = lambda values: self.fn(values)[variable_slice]
        links = set()
        return PartialLink(vars=vars, fn=fn, links=links)

    def shape(self):
        vars = self.vars
        fn = lambda values: self.fn(values).shape
        links = set()
        return PartialLink(vars=vars, fn=fn, links=links)

    def _flatten(self):
        return flatten_list([var._flatten() for var in self.vars]) + [self]
